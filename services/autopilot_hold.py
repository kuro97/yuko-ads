"""
Удержание (hold) кандидатов рангового правила автопилота.

Идея: ранговое правило (`services/decision_policy.py::score_and_decide`, ветка
«портфельный аутсайдер») паузит любую рекламу, которая оказалась внизу ранга,
даже если её ROMI близок к цели, расход ещё небольшой, а на встречах/квалах
есть потенциал оплат. Вместо немедленной паузы такой кандидат помещается в
hold-стейт на `hold_days` дней. Если за это время оплаты выросли — реклама
«пережила удержание» и сбрасывается (живёт дальше по обычным правилам). Если
оплат так и нет — паузится с явной причиной.

Удержание НЕ трогает подтверждённые сливы (is_confirmed_waster) и ранние
сигналы (день 1-3, is_early_waster) — они паузятся как раньше, без задержки.

Модуль изолирован от services/autopilot.py: там в эту же волну живёт другой
незакоммиченный дифф (_format_pause_report), поэтому логика удержания вынесена
отдельно и подключается извне (интеграция — отдельная задача T4).
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Часовой пояс CityA (UTC+5) — как в autopilot.py
_TZ_LOCAL = timezone(timedelta(hours=5))

# Абсолютный путь от корня проекта — cron может стартовать из другой директории
HOLD_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "autopilot_hold_state.json"

# Дефолты порогов удержания (мержатся поверх settings.autopilot внутри get_autopilot_config).
HOLD_DEFAULTS = {
    "hold_enabled": False,       # мастер-ключ. False = ранговое правило паузит как раньше
    "hold_romi_target": 200,     # цель ROMI (%), относительно которой считаем «близость»
    "hold_romi_ratio": 0.8,      # romi/target >= этого → условие (а) выполнено
    "hold_spend_max": 800,       # lifetime spend ($) <= этого → условие (б) выполнено
    "hold_min_qual_pct": 15,     # qual_pct >= этого → условие (в) выполнено (при payments==0)
    "hold_min_meetings": 2,      # meetings_scheduled+meetings_held >= этого → условие (в) тоже выполнено
    "hold_days": 7,              # длительность удержания в днях
}

# Во сколько раз короче держим кандидата, чью паузу усилил падающий тренд
# недельных когорт (волна 3). Не настройка, а правило — см. make_hold_entry.
_TREND_HOLD_DAYS_FACTOR = 0.5


def _parse_iso(iso_str: str | None) -> datetime:
    """Парсит ISO-строку в datetime с tz CityA. При ошибке/None → epoch (1970)."""
    if not iso_str:
        return datetime.fromtimestamp(0, tz=_TZ_LOCAL)
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ_LOCAL)
        return dt
    except (ValueError, TypeError):
        return datetime.fromtimestamp(0, tz=_TZ_LOCAL)


def load_hold_state() -> dict:
    """Загружает hold-стейт из HOLD_STATE_FILE и делает ролловер.

    Структура файла: {"holds": {ad_id: HoldEntry, ...}}
    HoldEntry = {
        "ad_name": str,
        "held_at": iso str,
        "hold_until": iso str,
        "payments_at_hold": int,   # payments на момент постановки в удержание (0 если None)
        "romi": float | None,
        "qual_pct": float | None,
        "spend": float,
        "reason": str,             # причина рангового правила, ради которой хотели паузить
    }

    Ролловер при загрузке: невалидные записи (нет hold_until / не парсится) удаляются.
    Возвращает {"holds": {...}} (всегда с ключом holds).
    """
    if not HOLD_STATE_FILE.exists():
        return {"holds": {}}
    try:
        data = json.loads(HOLD_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Не удалось загрузить autopilot_hold_state.json: %s", e)
        return {"holds": {}}

    holds = data.get("holds", {})
    if not isinstance(holds, dict):
        return {"holds": {}}

    # Ролловер: выкидываем записи без валидного hold_until
    valid_holds = {}
    for ad_id, entry in holds.items():
        if not isinstance(entry, dict):
            continue
        hold_until_raw = entry.get("hold_until")
        if not hold_until_raw:
            continue
        try:
            datetime.fromisoformat(hold_until_raw)
        except (ValueError, TypeError):
            continue
        valid_holds[ad_id] = entry

    return {"holds": valid_holds}


def save_hold_state(state: dict) -> None:
    """Атомарно сохраняет hold-стейт (tmp + replace), как _save_state в autopilot.py."""
    HOLD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HOLD_STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(HOLD_STATE_FILE)
    except Exception as e:
        logger.error("Не удалось сохранить autopilot_hold_state.json: %s", e)
        raise


def is_held(ad_id: str, state: dict) -> bool:
    """True если ad_id в state['holds'] и hold_until в будущем (now < hold_until)."""
    entry = state.get("holds", {}).get(ad_id)
    if not entry:
        return False
    hold_until = _parse_iso(entry.get("hold_until"))
    now = datetime.now(_TZ_LOCAL)
    return now < hold_until


def should_hold(
    decision: dict,
    ad_metrics: dict,
    cfg: dict,
) -> tuple[bool, str]:
    """Чистая функция: решает, надо ли поставить кандидата рангового правила в удержание.

    Args:
        decision   — запись из score_and_decide: {ad_id, ad_name, action, reasons,
                     is_confirmed_waster, is_early_waster, ...}
        ad_metrics — метрики объявления из локальной БД: {spend, romi, qual_pct, payments, ...}
        cfg        — конфиг автопилота (get_autopilot_config), содержит HOLD_DEFAULTS.

    Returns:
        (True, "")           — надо держать (все условия выполнены)
        (False, "причина")   — держать НЕ надо; причина на русском для лога/телеметрии.

    Правила (порядок важен — ранний return):
      0. hold_enabled == False                         → (False, "удержание выключено")
      1. decision.is_confirmed_waster == True          → (False, "слив — не держим")
      2. decision.is_early_waster == True              → (False, "ранний сигнал — не держим")
      3. Причина PAUSE НЕ ранговая (нет 'портфельный аутсайдер' в reasons)
                                                       → (False, "не ранговое правило")

      Условие (а) «ROMI близок к цели» применимо ТОЛЬКО когда есть хотя бы одна
      оплата (payments > 0). Без единой оплаты ROMI = 0 по определению
      (revenue == 0) и не несёт информации о качестве рекламы — требовать
      «ROMI близок к цели» в этом случае невозможно в принципе и убивает ровно
      тот кейс, ради которого создавалось удержание: маленький расход + хороший
      квал/встречи, но оплат ещё не было (решение владельца: иначе ранговое
      правило паузит дешёвую рекламу с хорошим квалом и нулём оплат, а hold
      не срабатывает с причиной «ROMI далёк от цели»).
      4. payments > 0 и romi is None                   → (False, "нет ROMI")
         payments > 0 и hold_romi_target == 0          → (False, "нет цели ROMI")
         payments > 0 и romi/hold_romi_target < hold_romi_ratio
                                                       → (False, "ROMI далёк от цели")
         payments == 0 (или None)                      → условие (а) пропускается,
                                                          решение отдаётся (б) и (в)
      5. spend > hold_spend_max                        → (False, "расход большой")
      6. НЕ (payments>0 или qual_pct>=hold_min_qual_pct или
             meetings_scheduled+meetings_held>=hold_min_meetings) → (False, "нет потенциала оплат")
      7. иначе                                         → (True, "")
    """
    # Правило 0: мастер-выключатель
    if not cfg.get("hold_enabled", HOLD_DEFAULTS["hold_enabled"]):
        return False, "удержание выключено"

    # Правило 1: подтверждённый слив не держим — паузим немедленно
    if decision.get("is_confirmed_waster"):
        return False, "слив — не держим"

    # Правило 2: ранний сигнал (день 1-3) не держим — прежняя логика
    if decision.get("is_early_waster"):
        return False, "ранний сигнал — не держим"

    # Правило 3: держим кандидатов рангового правила («портфельный аутсайдер»)
    # и кандидатов, чью паузу усилил падающий тренд недельных когорт (волна 3).
    # Тренд — основание ВОЙТИ в удержание (с укороченным таймером, см.
    # make_hold_entry), но НЕ основание его обойти: правила 1 и 2 выше стоят
    # раньше и тренд их не отменяет, а обхода удержания «раз тренд падает —
    # паузим немедленно» здесь нет и быть не должно.
    reasons = decision.get("reasons") or []
    is_rank_candidate = any("портфельный аутсайдер" in r for r in reasons)
    is_trend_candidate = bool(decision.get("trend_reinforced"))
    if not (is_rank_candidate or is_trend_candidate):
        return False, "не ранговое правило"

    # Правило 4 / условие (а): ROMI-гейт применяем ТОЛЬКО если уже есть оплаты —
    # без оплат romi всегда 0 и гейт неинформативен (см. докстринг выше).
    payments = ad_metrics.get("payments")
    has_payments = bool(payments and payments > 0)
    if has_payments:
        romi = ad_metrics.get("romi")
        if romi is None:
            return False, "нет ROMI"

        hold_romi_target = cfg.get("hold_romi_target", HOLD_DEFAULTS["hold_romi_target"])
        if not hold_romi_target:
            # Защита от деления на 0 — условие «а» проверить нельзя
            return False, "нет цели ROMI"

        hold_romi_ratio = cfg.get("hold_romi_ratio", HOLD_DEFAULTS["hold_romi_ratio"])
        romi_ratio = romi / hold_romi_target
        if romi_ratio < hold_romi_ratio:
            return False, "ROMI далёк от цели"

    # Условие (б): расход ещё не большой
    hold_spend_max = cfg.get("hold_spend_max", HOLD_DEFAULTS["hold_spend_max"])
    spend = ad_metrics.get("spend") or 0
    if spend > hold_spend_max:
        return False, "расход большой"

    # Условие (в): есть потенциал оплат — payments>0 (уже посчитано выше как
    # has_payments) ИЛИ qual_pct достаточен ИЛИ на встречах достаточно лидов
    # (фаза 2, ARCH-hold-meetings).
    # meetings_scheduled/meetings_held читаем через .get(..., 0) or 0 — если полей нет
    # (AMO лёг, старый вызов) или они None (гейт «FB-лидов 0» в _enrich_with_amo),
    # условие деградирует до payments/qual как в фазе 1 — обратная совместимость 100%.
    qual_pct = ad_metrics.get("qual_pct")
    hold_min_qual_pct = cfg.get("hold_min_qual_pct", HOLD_DEFAULTS["hold_min_qual_pct"])
    meetings_scheduled = ad_metrics.get("meetings_scheduled", 0) or 0
    meetings_held = ad_metrics.get("meetings_held", 0) or 0
    hold_min_meetings = cfg.get("hold_min_meetings", HOLD_DEFAULTS["hold_min_meetings"])
    has_qual_potential = qual_pct is not None and qual_pct >= hold_min_qual_pct
    has_meetings_potential = (meetings_scheduled + meetings_held) >= hold_min_meetings
    if not (has_payments or has_qual_potential or has_meetings_potential):
        return False, "нет потенциала оплат"

    return True, ""


def make_hold_entry(decision: dict, ad_metrics: dict, cfg: dict) -> dict:
    """Собирает HoldEntry (см. load_hold_state) с held_at=now, hold_until=now+hold_days.
    payments_at_hold = int(ad_metrics.get('payments') or 0).
    meetings_scheduled/meetings_held — снимок на момент постановки в удержание (0 если нет данных),
    meetings_at_hold — их сумма, для телеметрии и текста причины при истечении (ARCH-hold-meetings).

    Падающий тренд (decision.trend_reinforced, волна 3 недельных когорт)
    укорачивает таймер в _TREND_HOLD_DAYS_FACTOR раз, но не короче одного дня:
    ждать полную неделю там, где качество лидов падает три недели подряд,
    значит платить за наблюдение. Множитель — правило, а не настройка: ещё один
    денежный тумблер в settings ослабил бы гарантию «дефолт ничего не меняет»."""
    hold_days = cfg.get("hold_days", HOLD_DEFAULTS["hold_days"])
    if decision.get("trend_reinforced"):
        hold_days = max(1, int(hold_days * _TREND_HOLD_DAYS_FACTOR))
    now = datetime.now(_TZ_LOCAL)
    hold_until = now + timedelta(days=hold_days)

    # Причина рангового правила, ради которой хотели паузить — берём из decision.reasons
    # (как в _run_live_inner: reason = "; ".join(decision.get("reasons", [])))
    reason = "; ".join(decision.get("reasons") or [])

    meetings_scheduled = int(ad_metrics.get("meetings_scheduled", 0) or 0)
    meetings_held = int(ad_metrics.get("meetings_held", 0) or 0)

    return {
        "ad_name": decision.get("ad_name", ""),
        "held_at": now.isoformat(),
        "hold_until": hold_until.isoformat(),
        "payments_at_hold": int(ad_metrics.get("payments") or 0),
        "romi": ad_metrics.get("romi"),
        "qual_pct": ad_metrics.get("qual_pct"),
        "spend": ad_metrics.get("spend") or 0,
        "reason": reason,
        "meetings_scheduled": meetings_scheduled,
        "meetings_held": meetings_held,
        "meetings_at_hold": meetings_scheduled + meetings_held,
    }


def check_hold_expired(
    ad_id: str,
    ad_metrics: dict,
    state: dict,
) -> tuple[str, str]:
    """Проверяет истёкшее удержание для ad_id (вызывать когда now >= hold_until).

    Returns:
        ("survived", "")            — payments выросли (current > payments_at_hold) → сброс.
        ("pause", "удержание истекло, оплат нет (было N, стало M)")  — паузить.
        ("pause", "удержание истекло: было K встреч, оплат нет (было N, стало M)") —
            паузить, если на момент постановки в удержание были встречи
            (meetings_at_hold > 0 в entry) — обогащённый текст (ARCH-hold-meetings §9/AC-10).
    РЕШЕНИЕ (pause/survived) не зависит от встреч — только от роста оплат.
    Встречи влияют ТОЛЬКО на текст причины при pause.
    Не мутирует state (удаление записи — ответственность вызывающего)."""
    entry = state.get("holds", {}).get(ad_id, {})
    payments_at_hold = int(entry.get("payments_at_hold") or 0)

    current_payments = ad_metrics.get("payments")
    # Без сверки AMO нельзя утверждать что оплаты выросли — трактуем как 0 (не выросли)
    current = int(current_payments) if current_payments is not None else 0

    if current > payments_at_hold:
        return "survived", ""

    # meetings_at_hold отсутствует/0 → старый текст фазы 1 (обратная совместимость).
    meetings_at_hold = int(entry.get("meetings_at_hold") or 0)
    if meetings_at_hold > 0:
        return (
            "pause",
            f"удержание истекло: было {meetings_at_hold} встреч, "
            f"оплат нет (было {payments_at_hold}, стало {current})",
        )

    return (
        "pause",
        f"удержание истекло, оплат нет (было {payments_at_hold}, стало {current})",
    )
