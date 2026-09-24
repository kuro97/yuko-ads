"""
Страж бюджета 24/7 (Фаза 1) — services/guardian.py

Роль: НАБЛЮДАТЕЛЬ. Guardian сам не паузит объявления — реальные боевые паузы
идут через единственную точку `services.autopilot.run_autopilot_live`
(правила early_waster/wasted_no_crm внутри decision_policy.score_and_decide
переводят action в PAUSE, когда соответствующий dry_run снят, и
run_autopilot_live паузит их штатным путём — с guardrail, дневным лимитом,
перечиткой FB и Telegram-кнопками «Вернуть»).

Что делает guardian.py:
1. run_guardian_sweep — читает локальную БД, считает кандидатов
   early_waster/wasted_no_crm через score_and_decide, шлёт dry_run-сводку
   в Telegram, и защищает от «сломанной сверки CRM» (broken-sync guard).
2. guardian_time_to_pause_stats — метрика Стража для вечернего отчёта
   (среднее время слив→пауза, оценка спасённых денег за неделю).
3. Сторож свежести данных: mark_spend_refresh_ok / spend_refresh_age_hours —
   пишут/читают timestamp последнего успешного рефреша расхода
   (используются кроном _cron_data_freshness_watchdog в web/app.py).

См. docs/specs/ARCH-phase1-guardian.md §3, §6.4, §8.2, §8.4, §8.5.
"""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Локальный часовой пояс (UTC+5 по умолчанию, настраивается здесь)
_TZ_LOCAL = timezone(timedelta(hours=5))

# State-файл Стража (last_spend_refresh_at, freshness_alert_at, broken_sync_alert_at)
_GUARDIAN_STATE = Path(__file__).resolve().parent.parent / "data" / "guardian_state.json"

# Дедуп критического алерта «сверка сломана» — не чаще 1 раза в 6 часов
_BROKEN_SYNC_ALERT_COOLDOWN_HOURS = 6


# ---------------------------------------------------------------------------
# Дефолты Стража (мержатся в autopilot.guardian через get_guardian_config).
# Единственный источник правды для этих значений — здесь. decision_policy
# дублирует их в _GUARDIAN_RULE_DEFAULTS (комментарий там же) на случай если
# thresholds не передали guardian-ключи.
# ---------------------------------------------------------------------------
GUARDIAN_DEFAULTS: dict = {
    "enabled": False,          # мастер-ключ Стража (sweep). FALSE = только dry_run-логи в decision_policy не паузят.
    # Ранние сигналы (день 1-3)
    "early_dry_run": True,     # TRUE = early_waster не паузит, только помечает/логирует
    "early_min_age_hours": 24, # не трогать объявления моложе N часов (learning-фаза FB)
    "early_min_spend": 15.0,   # min расход для любого early-сигнала ($)
    "early_day1_zero_spend": 25.0,   # день 1: spend > X и 0 лидов → аномалия
    "early_cpl_mult": 3.0,     # день 2-3: CPL > mult × медианы города → аномалия
    "early_day23_min_spend": 20.0,   # день 2-3: min расход для CPL-аномалии ($)
    "early_qual_override_pct": 15.0,  # день 2-3: qual_pct >= порога → НЕ слив (дорогой, но качественный лид)
    # Остаточная дыра wasted_no_crm
    "wnc_dry_run": True,       # TRUE = wasted_no_crm не паузит, только помечает/алертит
    "wnc_min_spend": 150.0,
    "wnc_min_leads": 10,
    "wnc_min_days": 3,         # outcomes_matched_at NULL уже >= N дней (по days_running)
}


def get_guardian_config() -> dict:
    """Читает autopilot.guardian из settings.json, мержит поверх GUARDIAN_DEFAULTS."""
    from agent.scheduler import load_settings

    settings = load_settings()
    autopilot_cfg = settings.get("autopilot") or {}
    guardian_cfg = autopilot_cfg.get("guardian") or {}
    return {**GUARDIAN_DEFAULTS, **guardian_cfg}


# ---------------------------------------------------------------------------
# State Стража (JSON, tmp+rename — тот же паттерн, что и в autopilot.py)
# ---------------------------------------------------------------------------

def _load_guardian_state() -> dict:
    """{'last_spend_refresh_at': iso|None, 'freshness_alert_at': iso|None,
    'broken_sync_alert_at': iso|None}. Не бросает — при ошибке возвращает дефолт."""
    default = {
        "last_spend_refresh_at": None,
        "freshness_alert_at": None,
        "broken_sync_alert_at": None,
    }
    if not _GUARDIAN_STATE.exists():
        return default
    try:
        data = json.loads(_GUARDIAN_STATE.read_text(encoding="utf-8"))
        return {**default, **data}
    except Exception as exc:
        logger.warning("guardian: не удалось загрузить guardian_state.json: %s", exc)
        return default


def _save_guardian_state(state: dict) -> None:
    """Атомарно (tmp+rename). Не бросает — логирует."""
    try:
        _GUARDIAN_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _GUARDIAN_STATE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_GUARDIAN_STATE)
    except Exception as exc:
        logger.error("guardian: не удалось сохранить guardian_state.json: %s", exc)


# ---------------------------------------------------------------------------
# Сторож свежести данных
# ---------------------------------------------------------------------------

def mark_spend_refresh_ok(now: datetime | None = None) -> None:
    """Пишет last_spend_refresh_at=now в guardian_state. Вызывается из крона рефреша при успехе."""
    if now is None:
        now = datetime.now(_TZ_LOCAL)
    state = _load_guardian_state()
    state["last_spend_refresh_at"] = now.isoformat()
    _save_guardian_state(state)


def spend_refresh_age_hours(now: datetime | None = None) -> float | None:
    """Часов с последнего успешного рефреша. None если ещё не было прогона."""
    if now is None:
        now = datetime.now(_TZ_LOCAL)
    state = _load_guardian_state()
    last_iso = state.get("last_spend_refresh_at")
    if not last_iso:
        return None
    try:
        last = datetime.fromisoformat(last_iso)
    except Exception as exc:
        logger.warning("guardian: не удалось распарсить last_spend_refresh_at=%r: %s", last_iso, exc)
        return None
    # Приводим оба к aware datetime (наивные считаем локальными — так писали mark_spend_refresh_ok)
    if last.tzinfo is None:
        last = last.replace(tzinfo=_TZ_LOCAL)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_LOCAL)
    delta = now - last
    return delta.total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# Guardian sweep — главная функция Стража (наблюдатель + broken-sync guard)
# ---------------------------------------------------------------------------

def run_guardian_sweep(trigger: str = "cron") -> dict:
    """Главная функция Стража.

    Шаги (см. docs/specs/ARCH-phase1-guardian.md §3, §6.4, §8.2):
      1. get_guardian_config; ГЕЙТ kill_switch (из get_autopilot_config) → skipped.
      2. _fetch_ads_from_local_db() (переиспользуем shadow_report — 0 FB).
      3. score_and_decide(ads, thresholds+guardian) → у каждого есть is_early_waster / is_wasted_no_crm.
      4. BROKEN-SYNC GUARD: если len(ads)>0 и у ВСЕХ outcomes_matched_at is None →
         send_critical_alert('Страж: сверка AMO сломана', ...), НЕ паузить, return skipped='broken_sync'.
      5. Guardian НЕ паузит сам (см. §6.4 — единая точка пауз это run_autopilot_live,
         которая сама подхватит PAUSE-кандидатов, когда соответствующий dry_run снят).
         Здесь только собираем кандидатов для сводки/статистики.
      6. dry_run кандидаты (или боевые, но ещё не запауженные autopilot_live в этом
         прогоне) → Telegram «поймал бы X (причина)».
      7. return {'ran': bool, 'skipped': str|None, 'analyzed': int,
                 'early_candidates': [...], 'wnc_candidates': [...],
                 'paused': [ad_id], 'dry_run_only': [ad_id], 'errors': [...]}

    Никогда не бросает — верхний try/except логирует и возвращает {'ran': False, 'errors':[...]}.
    """
    try:
        return _run_guardian_sweep_inner(trigger=trigger)
    except Exception as exc:
        logger.exception("run_guardian_sweep: критическая ошибка (%s): %s", trigger, exc)
        return {
            "ran": False,
            "skipped": None,
            "analyzed": 0,
            "early_candidates": [],
            "wnc_candidates": [],
            "paused": [],
            "dry_run_only": [],
            "errors": [str(exc)],
        }


def _run_guardian_sweep_inner(trigger: str) -> dict:
    """Внутренняя реализация run_guardian_sweep (без верхнего try/except)."""
    from services.notifications import send_telegram, send_critical_alert

    empty_result = {
        "ran": False,
        "skipped": None,
        "analyzed": 0,
        "early_candidates": [],
        "wnc_candidates": [],
        "paused": [],
        "dry_run_only": [],
        "errors": [],
    }

    # Шаг 1: конфиг + ГЕЙТ kill_switch (общий с autopilot — единый предохранитель)
    from services.autopilot import get_autopilot_config

    autopilot_cfg = get_autopilot_config()
    if autopilot_cfg.get("kill_switch"):
        logger.info("run_guardian_sweep: kill_switch=true — пропускаем")
        return {**empty_result, "skipped": "kill_switch"}

    guardian_cfg = get_guardian_config()

    # Шаг 2: локальные данные (0 FB-запросов)
    from services.shadow_report import _fetch_ads_from_local_db

    ads = _fetch_ads_from_local_db()
    if not ads:
        logger.info("run_guardian_sweep: нет активных объявлений в локальной БД")
        return {**empty_result, "ran": True, "analyzed": 0}

    # Шаг 3: score_and_decide с объединёнными порогами (thresholds ∪ guardian_config)
    from services.decision_policy import score_and_decide
    from agent.scheduler import load_settings

    thresholds = dict(load_settings().get("thresholds") or {})
    combined_thresholds = {**thresholds, **guardian_cfg}
    decisions = score_and_decide(ads, combined_thresholds)

    # Шаг 4: BROKEN-SYNC GUARD — если сверка AMO сломана глобально, ловить
    # wasted_no_crm нельзя (массовая ложная тревога), только алертим.
    if all(ad.get("outcomes_matched_at") is None for ad in ads):
        state = _load_guardian_state()
        last_alert_iso = state.get("broken_sync_alert_at")
        should_alert = True
        if last_alert_iso:
            try:
                last_alert = datetime.fromisoformat(last_alert_iso)
                if last_alert.tzinfo is None:
                    last_alert = last_alert.replace(tzinfo=_TZ_LOCAL)
                now = datetime.now(_TZ_LOCAL)
                hours_since = (now - last_alert).total_seconds() / 3600.0
                should_alert = hours_since >= _BROKEN_SYNC_ALERT_COOLDOWN_HOURS
            except Exception as exc:
                logger.warning("guardian: не удалось распарсить broken_sync_alert_at: %s", exc)
                should_alert = True

        if should_alert:
            send_critical_alert(
                "🛑 Страж: сверка AMO сломана",
                f"У всех {len(ads)} активных объявлений нет матчей AMO — вероятно, сломан "
                "пайплайн сверки. Паузы wasted_no_crm отменены до восстановления.",
            )
            state["broken_sync_alert_at"] = datetime.now(_TZ_LOCAL).isoformat()
            _save_guardian_state(state)
        else:
            logger.info("run_guardian_sweep: broken_sync — алерт подавлен дедупом (<%dч)",
                        _BROKEN_SYNC_ALERT_COOLDOWN_HOURS)

        return {**empty_result, "ran": True, "analyzed": len(ads), "skipped": "broken_sync"}

    # Шаг 5: собираем кандидатов (флаги проставлены ВСЕГДА независимо от dry_run)
    early_candidates = [d for d in decisions if d.get("is_early_waster")]
    wnc_candidates = [d for d in decisions if d.get("is_wasted_no_crm")]

    # Кандидаты, у которых action уже стал PAUSE (dry_run снят где-то выше) — их
    # реально запаузит run_autopilot_live штатным путём (единая точка пауз, §6.4).
    # Guardian их не паузит сам, только относит к paused для отчётности sweep.
    paused_ids = [
        d["ad_id"] for d in decisions
        if d.get("action") == "PAUSE" and (d.get("is_early_waster") or d.get("is_wasted_no_crm"))
    ]
    dry_run_only_ids = [
        d["ad_id"] for d in (early_candidates + wnc_candidates)
        if d["ad_id"] not in paused_ids
    ]
    # Убираем дубли, сохраняя порядок
    dry_run_only_ids = list(dict.fromkeys(dry_run_only_ids))

    # Полная автономия пауз (режим «только итоги, не
    # предложения»): диагностические классы Стража (ранний слив / wasted_no_crm)
    # карточками НЕ едут — иначе страж-инвариант (services/autonomy_invariant)
    # считает каждую такую карточку протечкой и алертит каждые 15 минут
    # (шторм алертов). Находки остаются в логе и в результате sweep;
    # решение по ним принимает автопилот со своими правилами.
    suppressed_by_autonomy: list[str] = []
    try:
        from services.autonomous_pause import is_full_pause_autonomy_enabled

        if dry_run_only_ids and is_full_pause_autonomy_enabled():
            suppressed_by_autonomy = list(dry_run_only_ids)
            dry_run_only_ids = []
            logger.info(
                "run_guardian_sweep: полная автономия — %d диагностических "
                "кандидатов Стража без карточек: %s",
                len(suppressed_by_autonomy),
                suppressed_by_autonomy,
            )
    except Exception as exc:  # noqa: BLE001 — сбой чтения конфига = старое поведение
        logger.warning("run_guardian_sweep: проверка автономии пропущена — %s", exc)

    # Шаг 6: каждому кандидату — штатная карточка с цифрами и кнопками.
    #
    # Раньше здесь была голая сводка «поймал бы N» без данных и без кнопки —
    # владелец видел находку и не мог на неё нажать.
    # Теперь Страж — такой же продюсер предложений, как автопилот: карточка
    # проходит общий путь (дедуп живых дублей, последний ACTIVE не трогается,
    # инвентарь), решает владелец. «Поймал бы» уходит только про то, что
    # карточкой не стало (дубль уже висит, последняя активная и т.п.).
    if dry_run_only_ids:
        from services.action_producer_gateway import (
            DecisionContext,
            ProducerActionError,
            propose_pause,
        )
        from services.approval_checker_models import ActionOrigin

        by_id = {d["ad_id"]: d for d in decisions}
        sweep_id = str(uuid.uuid4())
        carded = 0
        leftovers: list[str] = []
        for ad_id in dry_run_only_ids[:10]:
            d = by_id.get(ad_id, {})
            kind = "ранний слив" if d.get("is_early_waster") else "wasted_no_crm"
            try:
                outcome = propose_pause(
                    ad_id,
                    origin=ActionOrigin.AUTOPILOT_LIVE,
                    scope=f"guardian:{sweep_id}:{ad_id}",
                    reason_code=(
                        "GUARDIAN_EARLY_WASTER"
                        if d.get("is_early_waster")
                        else "GUARDIAN_WASTED_NO_CRM"
                    ),
                    decision=DecisionContext(
                        spend_usd=d.get("spend"),
                        leads=d.get("leads"),
                        cpl_usd=d.get("cpl"),
                        qual_pct=d.get("qual_pct"),
                        payments=d.get("payments"),
                        business_reason=f"Страж: {kind}",
                    ),
                )
                if outcome.receipt is not None:
                    carded += 1
                    continue
            except ProducerActionError as exc:
                leftovers.append(f"— {d.get('ad_name', ad_id)}: {kind} ({exc})")
            except Exception as exc:  # noqa: BLE001 — один кандидат не рвёт sweep
                logger.warning(
                    "run_guardian_sweep: карточка %s не создана — %s", ad_id, exc
                )
                leftovers.append(f"— {d.get('ad_name', ad_id)}: {kind}")
        lines: list[str] = []
        if carded or leftovers:
            lines.append(
                f"🐢 <b>Страж</b> ({trigger}): отправил {carded} "
                "предложений паузы — карточки с цифрами уже в чате"
            )
        if leftovers:
            lines.append("Карточкой не стало:")
            lines.extend(leftovers)
        if len(dry_run_only_ids) > 10:
            lines.append(f"...и ещё {len(dry_run_only_ids) - 10}")
        if lines:
            send_telegram("\n".join(lines))

    return {
        "ran": True,
        "skipped": None,
        "analyzed": len(ads),
        "early_candidates": [d["ad_id"] for d in early_candidates],
        "wnc_candidates": [d["ad_id"] for d in wnc_candidates],
        "paused": paused_ids,
        "dry_run_only": dry_run_only_ids,
        "suppressed_by_autonomy": suppressed_by_autonomy,
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Метрика Стража (для вечернего отчёта)
# ---------------------------------------------------------------------------

# Источники, чьи PAUSED-решения считаются «действием Стража/автопилота»
_GUARDIAN_METRIC_CONFIRMED_BY = ("autopilot", "guardian")


def guardian_time_to_pause_stats(days: int = 7) -> dict:
    """Метрика Стража за последние `days` дней (для вечернего отчёта).

    Читает decisions (action='PAUSED', confirmed_by IN ('autopilot','guardian')) за период,
    для каждого считает 'время слив→пауза' = created_at паузы минус момент, когда объявление
    стало сливом (аппроксимация: первая дата в ad_daily_metrics, где кумулятивный spend
    пробил порог waster_min_spend=150 ИЛИ early_min_spend для ранних). Если нет метрик —
    объявление пропускается из time-to-pause (в saved_usd учитывается по decisions.spend).

    Returns:
      {'pauses': int, 'avg_hours_to_pause': float|None, 'saved_usd_week': float,
       'sample': [{'ad_id','ad_name','hours_to_pause','spend'}]}
    saved_usd_week = сумма decisions.spend запауженных сливов за 7д (расход, который иначе
    крутился бы дальше; экстраполяция на неделю = та же сумма, т.к. окно уже недельное).
    Никогда не бросает.
    """
    empty_stats = {
        "pauses": 0,
        "avg_hours_to_pause": None,
        "saved_usd_week": 0.0,
        "sample": [],
    }
    try:
        return _guardian_time_to_pause_stats_inner(days)
    except Exception as exc:
        logger.warning("guardian_time_to_pause_stats: ошибка (%s) — возвращаю нулевую структуру", exc)
        return empty_stats


def _guardian_time_to_pause_stats_inner(days: int) -> dict:
    """Внутренняя реализация guardian_time_to_pause_stats (без верхнего try/except)."""
    from services.creative_intelligence import DB_PATH

    if DB_PATH is None:
        logger.warning("guardian_time_to_pause_stats: DB_PATH не инициализирован")
        return {"pauses": 0, "avg_hours_to_pause": None, "saved_usd_week": 0.0, "sample": []}

    now = datetime.now(_TZ_LOCAL)
    date_from = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in _GUARDIAN_METRIC_CONFIRMED_BY)
        rows = conn.execute(
            f"""
            SELECT ad_id, ad_name, created_at, spend
            FROM decisions
            WHERE action = 'PAUSED'
              AND confirmed_by IN ({placeholders})
              AND created_at >= ?
            ORDER BY created_at DESC
            """,
            (*_GUARDIAN_METRIC_CONFIRMED_BY, date_from),
        ).fetchall()

        # Паузы НОВОГО контура одобрений (с его вводом старая таблица decisions
        # не наполняется — без этого источника метрика молчала нулями).
        # Момент паузы = completed_at подтверждённой попытки; «остановленный
        # расход» = расход объявления за 7 дней до паузы (недельный темп,
        # который иначе продолжился бы), а не lifetime-spend старой таблицы.
        contour_rows: list[dict] = []
        try:
            since_utc = (now - timedelta(days=days)).astimezone(timezone.utc).isoformat()
            for r in conn.execute(
                """
                SELECT a.resource_id AS ad_id, a.completed_at
                FROM owner_action_attempts a
                WHERE a.operation_kind = 'PAUSE_AD' AND a.state = 'CONFIRMED'
                  AND a.completed_at >= ?
                ORDER BY a.completed_at DESC
                """,
                (since_utc,),
            ):
                ad_id = str(r["ad_id"] or "")
                if not ad_id:
                    continue
                try:
                    paused_at = datetime.fromisoformat(str(r["completed_at"])).astimezone(
                        _TZ_LOCAL
                    ).replace(tzinfo=None)
                except Exception:
                    continue
                day = paused_at.date()
                week_spend = conn.execute(
                    "SELECT COALESCE(SUM(spend), 0) FROM ad_daily_metrics "
                    "WHERE ad_id = ? AND date >= ? AND date < ?",
                    (ad_id, (day - timedelta(days=7)).isoformat(), day.isoformat()),
                ).fetchone()[0]
                name_row = None
                try:
                    name_row = conn.execute(
                        "SELECT ad_name FROM creative_kb WHERE ad_id = ?", (ad_id,)
                    ).fetchone()
                except sqlite3.OperationalError:
                    name_row = None
                contour_rows.append(
                    {
                        "ad_id": ad_id,
                        "ad_name": (name_row["ad_name"] if name_row else "") or "",
                        "paused_at": paused_at,
                        "spend": float(week_spend or 0),
                    }
                )
        except sqlite3.OperationalError:
            contour_rows = []  # схема без контура (старые БД, тестовые фикстуры)

        seen_ids = {r["ad_id"] for r in contour_rows}
        legacy_rows = [r for r in rows if str(r["ad_id"]) not in seen_ids]
        if not contour_rows and not legacy_rows:
            return {"pauses": 0, "avg_hours_to_pause": None, "saved_usd_week": 0.0, "sample": []}

        # Пороги для аппроксимации «момента, когда стал сливом»
        guardian_cfg = get_guardian_config()
        waster_min_spend = 150.0  # то же значение, что в decision_policy confirmed_waster (тир B)
        early_min_spend = float(guardian_cfg.get("early_min_spend", 15.0))

        sample: list[dict] = []
        hours_list: list[float] = []
        saved_usd_week = 0.0

        unified: list[dict] = list(contour_rows)
        for row in legacy_rows:
            try:
                legacy_paused_at = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                legacy_paused_at = None
            unified.append(
                {
                    "ad_id": row["ad_id"],
                    "ad_name": row["ad_name"],
                    "paused_at": legacy_paused_at,
                    "spend": float(row["spend"] or 0),
                }
            )

        for row in unified:
            ad_id = row["ad_id"]
            spend = float(row["spend"] or 0)
            saved_usd_week += spend
            paused_at = row["paused_at"]

            hours_to_pause = None
            if paused_at is not None:
                waster_at = _find_became_waster_at(conn, ad_id, waster_min_spend, early_min_spend)
                if waster_at is not None:
                    delta_hours = (paused_at - waster_at).total_seconds() / 3600.0
                    if delta_hours >= 0:
                        hours_to_pause = delta_hours
                        hours_list.append(delta_hours)

            sample.append({
                "ad_id": ad_id,
                "ad_name": row["ad_name"],
                "hours_to_pause": hours_to_pause,
                "spend": spend,
            })

        avg_hours_to_pause = (sum(hours_list) / len(hours_list)) if hours_list else None

        return {
            "pauses": len(unified),
            "avg_hours_to_pause": avg_hours_to_pause,
            "saved_usd_week": saved_usd_week,
            "sample": sample,
        }
    finally:
        conn.close()


def _find_became_waster_at(
    conn: sqlite3.Connection, ad_id: str, waster_min_spend: float, early_min_spend: float,
) -> datetime | None:
    """Первая дата в ad_daily_metrics, где кумулятивный spend объявления пробил
    минимальный из порогов (early_min_spend ИЛИ waster_min_spend — берём меньший,
    чтобы поймать и ранние, и зрелые сливы). Возвращает datetime (00:00 в день пробоя)
    или None если данных нет / порог не пробит.
    """
    threshold = min(waster_min_spend, early_min_spend)
    rows = conn.execute(
        "SELECT date, spend FROM ad_daily_metrics WHERE ad_id = ? ORDER BY date ASC",
        (ad_id,),
    ).fetchall()
    if not rows:
        return None

    cumulative = 0.0
    for row in rows:
        cumulative += float(row["spend"] or 0)
        if cumulative >= threshold:
            try:
                return datetime.strptime(row["date"], "%Y-%m-%d")
            except Exception:
                return None
    return None
