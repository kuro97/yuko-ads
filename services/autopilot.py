"""
Автопилот — ядро замкнутого цикла управления рекламой.

Логика:
1. Читает конфиг из data/settings.json (блок "autopilot")
2. Загружает метрики из FB + AMO
3. Применяет Decision Tree к каждому активному объявлению
4. В режиме dry_run — логирует без действий
5. В режиме active — ставит объявления на паузу (только PAUSED, никогда DELETE)

RAIL: без AMO данных паузы не выполняются — бот может не знать ROMI,
и ошибочно отключит прибыльную рекламу.
"""

import html
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

# Дефолты порогов «удержания» рангового правила (ARCH-rank-pause-hold) —
# домешиваются в AUTOPILOT_DEFAULTS ниже. Отдельный модуль, т.к. в autopilot.py
# в эту же волну живёт другой незакоммиченный дифф (_format_pause_report).
from services.autonomous_pause import AUTONOMOUS_DEFAULTS
from services.autopilot_hold import HOLD_DEFAULTS
from services.formatting import fmt_money, truncate_at_word_boundary, pluralize_leads
# Дефолты чистки слотов (архивация давно паузнутых) — источник правды лежит
# рядом с самим чистильщиком, здесь только домешивается.
from services.slot_cleaner import SLOT_CLEANER_DEFAULTS
# Дефолт режима тренда недельных когорт (волна 3) — единственный источник
# правды живёт рядом с самим гейтом, здесь он только домешивается в дефолты.
from services import trend_gate
from services.trend_gate import TREND_DEFAULTS

logger = logging.getLogger(__name__)


def _send_critical_alert_non_throwing(send_fn, title: str, detail: str) -> bool:
    """Уведомление не должно менять уже принятое fail-closed решение guard."""
    try:
        return bool(send_fn(title, detail))
    except Exception as exc:
        logger.error(
            "Не удалось отправить critical alert '%s': %s",
            title,
            type(exc).__name__,
        )
        return False

# Локальный часовой пояс (UTC+5 по умолчанию, настраивается здесь)
_TZ_LOCAL = timezone(timedelta(hours=5))

# Абсолютный путь от корня проекта — cron может стартовать из другой директории
STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "autopilot_state.json"

# Файл авто-действий (журнал, совместимый с agent/scheduler.py)
_AUTO_ACTIONS_FILE = Path(__file__).resolve().parent.parent / "data" / "auto_actions.json"

# State-файл дневного счётчика пауз для run_autopilot_live
_LIVE_DAILY_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "autopilot_live_daily.json"

# State-файл дневного счётчика пауз для классического автопилота (D3).
# Отдельный от LIVE-счётчика — контуры паузят независимо, у каждого свой кап 6/день.
_CLASSIC_DAILY_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "autopilot_classic_daily.json"

# Диск-кэш аналитики (пишется web/app.py при каждом успешном refresh)
_DISK_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "analytics_cache.json"

# Максимально допустимый возраст кэша для fallback в автопилоте (часы)
_CACHE_MAX_AGE_HOURS = 3

# Ретеншн карты undo_map «ad_id → {ad_name, returned, at}» для кнопок
# «↩️ Вернуть» в отчётах пауз (классика run_autopilot + Live run_autopilot_live).
# Зеркало STOP_MAP_RETENTION_DAYS/MAX_STOP_ENTRIES из services/auto_launch.py.
UNDO_MAP_RETENTION_DAYS = 30
MAX_UNDO_ENTRIES = 200

# Максимум кнопок «↩️ Вернуть» в одном отчёте пауз (не заваливать чат простынёй
# кнопок при массовой паузе — решение владельца).
MAX_PAUSE_UNDO_BUTTONS = 10

# Дефолтный конфиг автопилота
AUTOPILOT_DEFAULTS = {
    "enabled": False,
    "kill_switch": False,
    "mode": "dry_run",
    "max_pauses_per_run": 6,  # дефолт для одного прогона
    "max_pauses_per_day": 6,  # дневной потолок суммарно по всем прогонам
    "min_hours_between_runs": 3,
    "min_days_protect": 5,
    # Авто-запуск новых карточек (отдельный предохранитель)
    "launch_enabled": False,       # false = боевые запуски выключены даже при enabled=true
    "max_launches_per_day": 1,     # не более 1 авто-запуска в день
    # Checker запуска управляется только серверными settings. Observe не
    # выдаёт разрешение на создание рекламы до отдельного rollout enforce.
    "launch_checker": {
        "mode": "observe",
    },
    # -----------------------------------------------------------------------
    # Budget Scaler — масштабирование бюджетов победителей
    # ОПАСНО: увеличивает реальные расходы. Все дефолты консервативны.
    # -----------------------------------------------------------------------
    "scale_enabled": False,              # мастер-ключ (FALSE = только dry_run рекомендации)
    "max_budget_increase_pct": 15,       # максимальный % роста бюджета В СУТКИ на адсет (правило владельца: не более 15%/день)
    "max_adset_budget_mult": 2.0,        # не выше X × текущий бюджет
    "max_adset_daily_budget": 300,       # абсолютный потолок одного адсета ($)
    "max_total_daily_budget": 4000,      # потолок суммарного бюджета всех адсетов ($)
    "max_scales_per_run": 2,             # максимум адсетов за прогон
    # -----------------------------------------------------------------------
    # Budget Scaler v2 — флаги дополнительных предохранителей (по итогам
    # ревью). Единственный источник правды дефолтов v2; значения ЗЕРКАЛЯТ
    # fail-safe .get(...)-дефолты в services/budget_scaler.py (waster_min_spend_usd,
    # require_fresh_7d, engine_selfcheck_enabled, sanity_cap_enabled,
    # sanity_cap_ratio, selfcheck_max_dev). Дефолты консервативны: safety-гейты
    # (selfcheck/sanity-cap) ВКЛЮЧЕНЫ, а active-мутации не включаются сами
    # (require_fresh_7d=False = совместимость, честный 7d — осознанное решение
    # оператора). НИ ОДИН из этих ключей НЕ отключает строгое confirmed_waster-вето.
    # -----------------------------------------------------------------------
    "scaler_v2": {
        "waster_min_spend_usd": 10.0,      # порог значимости слива, $ (_waster_min_spend_usd)
        "require_fresh_7d": False,         # честный 7d-гейт active-подъёма (fail-closed), дефолт off
        "engine_selfcheck_enabled": True,  # самопроверка прогноза CDP (Этап 4), safety ON
        "sanity_cap_enabled": True,        # стоп-кран сезонной нормы (Этап 4), safety ON
        "sanity_cap_ratio": 0.7,           # порог стоп-крана — доля сезонной нормы (0.1..1.0)
        "selfcheck_max_dev": 0.25,         # макс. относительное отставание факта до недоверия (0.05..0.9)
    },
    # -----------------------------------------------------------------------
    # Adset Cleaner — ночная чистка старых PAUSED объявлений в полных адсетах.
    # ОПАСНО: удаление необратимо. Все дефолты ВЫКЛЮЧЕНЫ + dry_run.
    # -----------------------------------------------------------------------
    "cleaner": {
        "enabled": False,       # legacy мастер-ключ: active cleaner выключен
        "dry_run": True,        # ежедневный proactive-контур только read-only
        "proactive_enabled": False,
        "stale_days": 15,       # PAUSED возрастом N+ полных дней = кандидат
        "adset_threshold": 45,  # обязан равняться 50 - target_free
        "target_free": 5,
        "critical_free_slots": 2,
        "hard_reserve_slots": 1,
        "allow_irreversible_delete": False,
        "max_manifest_candidates_per_adset": 10,
        "max_deletes_per_workflow": 2,
        "alert_dedup_hours": 6,
        "managed_account_kinds": ["offline"],
    },
    # Replacement workflow выключен до отдельного ручного rollout.
    "replacement": {
        "enabled": False,
        "verify_interval_minutes": 30,
        "max_pending_hours": 48,
    },
    # Recovery по завершённым Trello-карточкам выключен до ручного rollout.
    "recovery": {
        "enabled": False,
        "since": "2026-07-01T00:00:00+05:00",
        "max_cards_per_day": 1,
        "managed_account_kinds": ["offline", "online"],
    },
    # -----------------------------------------------------------------------
    # Страж бюджета 24/7 (Фаза 1) — см. docs/specs/ARCH-phase1-guardian.md.
    # Единственный источник правды для значений — services.guardian.GUARDIAN_DEFAULTS,
    # копия нужна здесь, чтобы get_autopilot_config() отдавал полный блок "guardian"
    # даже при пустом settings.json (без импорта guardian на этапе загрузки модуля).
    # -----------------------------------------------------------------------
    "guardian": {
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
    },
    # -----------------------------------------------------------------------
    # Ранний стоп «расход ≥ N цен лида без заявки AMO» — см. services/early_kill.py.
    # Единственный источник
    # правды значений — services.early_kill.EARLY_KILL_DEFAULTS; копия нужна,
    # чтобы get_autopilot_config() отдавал полный блок и фильтровал ключи.
    # mode: off | shadow (active — волна 2, валидатор его пока не принимает).
    # -----------------------------------------------------------------------
    "early_kill": {
        "mode": "shadow",
        "max_age_days": 14,
        "max_per_day": 10,
        "floor_usd": 8.0,
        "cap_usd": 80.0,
        "cpl_window_days": 14,
        "cpl_min_leads": 10,
        "multiplier_no_lead": 3.0,
        "multiplier_one_lead": 4.5,
        "account_multipliers": {
            "152882611033373": {"no_lead": 4.0, "one_lead": 6.0},
        },
        # Правило B «зрелый ноль» (волна 2a) — см. services/early_kill.py
        "b_enabled": True,
        "b_min_mature_leads": 3,
        "b_maturity_hours": 72,
        "c_price_mode": "median",
        "c_cpq_norm_mult": 1.2,
        "c_price_min_spend_mult": 2.0,
        "c_price_min_mature": 5,
        "c_cpq_norm_min_quals": 10,
        "c_cpq_norm_fallback_usd": 100.0,
        "c_cpq_plank_floor_usd": 120.0,
        "b_ladder": None,
        "b_ladder_accounts": {},
        "b_tail_min_leads": 25,
        "b_tail_norm_share": None,
        "b_qual_norm_fallback_pct": 16.0,
        "b_qual_norm_min_leads": 100,
        "b_min_spend_usd": 25.0,
        "b_cpl_multiplier": 2.0,
        "b_small_max_leads": 2,
        "b_small_cpl_multiplier": 3.0,
        "op_guard_enabled": True,
        # Волна 2b: mode (правило A) принимает active, правило B — свой режим,
        # пока только off | shadow. Маркеры исключают объявления из правила B:
        # intl_line — другая воронка, квал там редкость.
        "b_mode": "shadow",
        "b_exclude_name_markers": ["intl"],
        # Волна 2c: правило C «квалы дорогие/выродились» и правило S «голодные» — раз в сутки.
        "c_mode": "shadow",
        "c_min_mature_leads": 30,
        "c_max_qual_pct": 10.0,
        "c_cpq_multiplier": 2.0,
        "c_cpq_fallback_usd": {"29716040622546856": 50.0, "152882611033373": 80.0},
        "s_mode": "shadow",
        "s_min_age_days": 3,
        "s_max_spend_usd": 15.0,
        "s_adset_min_active": 15,
        "s_max_per_day": 30,
        "daily_hour": 10,
    },
    # -----------------------------------------------------------------------
    # Аналитик-Гипотезник (Фаза 4) — см. docs/specs/ARCH-phase4-hypothesist.md.
    # Безопасный контур: только запись уроков и мягкая перестановка приоритетов
    # тем в topic_selector, денежных действий нет. enabled=True по умолчанию.
    # -----------------------------------------------------------------------
    "hypothesist": {
        "enabled": True,           # мастер-ключ. FALSE = журнал/вердикт/влияние/отчёт выключены
        "min_age_days": 7,         # гипотезу младше N дней вердикт не трогает
        "max_age_days": 14,        # старше N дней при недостатке данных закрывается inconclusive
        "min_spend": 15.0,         # минимальный расход для вынесения вердикта (HYP_MIN_SPEND)
        "segment_qual_min": 12.0,  # порог qual_pct для нового города/сегмента без медианы CPL (SEGMENT_QUAL_MIN)
        "ttl_days": 60,            # TTL вердикта для влияния на topic_selector (HYP_TTL_DAYS) — старше «реабилитируется»
        "dead_min_refuted": 2,     # refuted >= N и confirmed == 0 → комбо считается «мёртвым» (HYP_DEAD_MIN_REFUTED)
    },
    # -----------------------------------------------------------------------
    # Утренний дайджест (Фаза 5) — см. docs/specs/ARCH-phase5-awareness.md.
    # Read-only: одно сообщение в Telegram утром, никаких мутаций.
    # -----------------------------------------------------------------------
    "morning_digest": {
        "enabled": True,  # мастер-ключ. FALSE = _cron_morning_digest ничего не шлёт
    },
    # -----------------------------------------------------------------------
    # CDP Acme — источник юнитки для Бюджет-пилота (ДРР+план-гейт из CDP API
    # вместо ручного Google-листа).
    # enabled=True — решение владельца: CDP основной источник,
    # Google-лист только fallback (если CDP лёг).
    # doubt_alerts — протокол сомнений (Шаг A.2, ARCH-cdp-seasonal-pacing):
    # не блокирующие Telegram-уведомления о сомнительных решениях. Дефолт true.
    # pace_engine — движок budget-context (Шаг A.3, ARCH-cdp-budget-context)
    # как основной источник темпа для план-гейта. Дефолт true; false = темп
    # решает только сезонная кривая A.2 (движок при этом вообще не дёргается).
    # payments_source — источник факта оплаты по объявлению (Шаг B,
    # ARCH-cdp-payments): "amo" — только статусы сделок AMO (старый путь);
    # "erp" — боевой, объединённый сигнал max(AMO, ERP) через payments_effective;
    # "shadow" (дефолт) — решения по AMO как сейчас, но ERP считается и логируется
    # для короткой сверки перед переключением на "erp" вручную.
    # -----------------------------------------------------------------------
    "cdp": {"enabled": True, "doubt_alerts": True, "pace_engine": True, "payments_source": "shadow"},
    # -----------------------------------------------------------------------
    # Сторож кронов (heartbeat watchdog, Фаза 5) — read-only, шлёт Telegram-алерт
    # о молчащих кронах. Ничего не мутирует.
    # -----------------------------------------------------------------------
    "cron_watchdog": {
        "enabled": True,  # мастер-ключ. FALSE = run_cron_watchdog ничего не шлёт
    },
    # -----------------------------------------------------------------------
    # Алерты аномалий (Фаза 5) — CPL-спайк по городу и серия FB-ошибок.
    # Read-only, только Telegram-алерты на health-канал. Расход обслуживает
    # CDP completed-day контур services.cdp_spend_alerts в основном боте.
    # -----------------------------------------------------------------------
    "anomaly_alerts": {
        "enabled": True,          # мастер-ключ. FALSE = run_anomaly_alerts ничего не шлёт
        "cpl_mult": 3.0,          # CPL дня по городу > mult × медианы 7 дней → алерт
        "cpl_min_spend": 20.0,    # min расход дня города для CPL-алерта ($), отсекаем копейки
        # Legacy no-op: оставлены, чтобы старые settings.json продолжали приниматься.
        # Superseded CDP-логикой; новые spend-пороги отсюда не читаются.
        "spend_spike_mult": 1.5,
        "spend_spike_min_elapsed_hours": 12,
        "fb_error_burst_threshold": 5,        # серия FB-ошибок >= N за час → алерт
        "dedup_hours": 6,         # дедуп повторных алертов по одному ключу (часы)
    },
    # -----------------------------------------------------------------------
    # Страж просроченных офферов — ночной крон ищет активные объявления с
    # истёкшей датой оффера («до 15 июня», «DD.MM») в имени/тексте креатива,
    # которые ВСЁ ЕЩЁ жгут бюджет (пример: «до 15 июня» крутится весь следующий месяц).
    # Read-only: только Telegram-алерт с кнопками «⏸ Остановить», паузит человек.
    # dedup_hours — дедуп повторного алерта по одному объявлению.
    # -----------------------------------------------------------------------
    "expired_offer_guard": {
        "enabled": True,  # мастер-ключ (дефолт включён). FALSE = крон ничего не шлёт
        "dedup_hours": 12,  # не повторять алерт по одному ad_id чаще, чем раз в N часов
    },
    # -----------------------------------------------------------------------
    # Страж трат адсетов (services/adset_spend_guard.py) — утренний наблюдатель:
    # по эффективно-активным leadgen-адсетам с daily_budget>0 ловит вчерашний
    # $0-расход («не тратит») и перерасход (> бюджет × overspend_mult). Read-only:
    # только Telegram-сводка, ничего не паузит и бюджеты не трогает.
    # -----------------------------------------------------------------------
    "spend_guard": {
        "enabled": True,        # мастер-ключ. FALSE = крон ничего не шлёт
        "overspend_mult": 1.25,  # вчера потрачено > бюджет × mult → «перетрачивает»
    },
    # -----------------------------------------------------------------------
    # Ежедневный онлайн-отчёт из CDP (services/online_report.py) — одно Telegram
    # по псевдогороду «Онлайн»: расход/лиды/квалы/ДРР за вчера и месяц. Read-only.
    # -----------------------------------------------------------------------
    "online_report": {
        "enabled": True,  # мастер-ключ. FALSE = крон ничего не шлёт
    },
    # -----------------------------------------------------------------------
    # Автономные действия бота (services/autonomous_pause.py). Решение
    # владельца: ровно ОДИН класс — пауза подтверждённого слива.
    # Дефолт pause_confirmed_wasters=False: после деплоя поведение не меняется,
    # включает владелец через настройки без деплоя.
    # -----------------------------------------------------------------------
    **AUTONOMOUS_DEFAULTS,
    # -----------------------------------------------------------------------
    # Удержание рангового правила (ARCH-rank-pause-hold) — не паузить сразу
    # «почти-прибыльную» рекламу (портфельный аутсайдер с ROMI близко к цели),
    # а подержать hold_days и посмотреть на рост оплат. См. services/autopilot_hold.py.
    # -----------------------------------------------------------------------
    **HOLD_DEFAULTS,
    # -----------------------------------------------------------------------
    # Тренд недельных когорт (волна 3, services/trend_gate.py). Дефолт shadow:
    # тренд считается и попадает в отчёты, но НЕ меняет ни одного исхода.
    # Перевод в active — решение владельца через настройки, без деплоя.
    # -----------------------------------------------------------------------
    **TREND_DEFAULTS,
    # -----------------------------------------------------------------------
    # Чистка слотов адсета (services/slot_cleaner.py): архивация давно
    # паузнутых объявлений, чтобы новым креативам было куда ложиться. Дефолт
    # выключен — включает владелец через настройки, без деплоя.
    # -----------------------------------------------------------------------
    **SLOT_CLEANER_DEFAULTS,
}


def get_autopilot_config() -> dict:
    """Читает блок 'autopilot' из settings.json и мержит поверх дефолтов.

    Legacy-флаг auto_apply НЕ читается — он независим от автопилота v2.
    """
    from agent.scheduler import load_settings
    settings = load_settings()
    stored_raw = settings.get("autopilot")
    stored = stored_raw if isinstance(stored_raw, dict) else {}
    cfg = {**AUTOPILOT_DEFAULTS, **stored}
    # Safety-вложенности мержим отдельно: частичный settings не должен стирать
    # dry-run/delete defaults или возвращать legacy target_free=15.
    for nested_key in (
        "cleaner", "replacement", "recovery", "launch_checker", "trend", "autonomous",
        "slot_cleaner", "early_kill",
    ):
        nested = stored.get(nested_key)
        # Не пропускаем в runtime legacy/unknown ключи. В частности,
        # max_deletes_per_run больше не является разрешением на DELETE.
        allowed = AUTOPILOT_DEFAULTS[nested_key]
        known_nested = (
            {key: value for key, value in nested.items() if key in allowed}
            if isinstance(nested, dict)
            else {}
        )
        cfg[nested_key] = {**allowed, **known_nested}
    if cfg["launch_checker"].get("mode") not in ("observe", "enforce"):
        # Файл мог быть изменён в обход HTTP-валидатора. Не допускаем, чтобы
        # неизвестное значение случайно стало разрешающим режимом.
        cfg["launch_checker"]["mode"] = "observe"
    if cfg["trend"].get("mode") not in ("off", "shadow", "active"):
        # Та же дисциплина для тренда: неизвестное значение из правленого
        # руками settings.json не должно случайно стать боевым режимом.
        cfg["trend"]["mode"] = "shadow"
    if cfg["early_kill"].get("mode") not in ("off", "shadow", "active"):
        # Ранний стоп: мусор в mode → выключено, а не наблюдение и не бой.
        cfg["early_kill"]["mode"] = "off"
    for mode_key in ("b_mode", "c_mode", "s_mode"):
        if cfg["early_kill"].get(mode_key) not in ("off", "shadow", "active"):
            cfg["early_kill"][mode_key] = "off"
    if cfg.get("kill_switch") is True:
        # Общий стоп-кран доминирует над сохранёнными mutation-флагами, но не
        # выключает read-only proactive scan для наблюдения за pressure.
        cfg["cleaner"] = {
            **cfg["cleaner"],
            "enabled": False,
            "dry_run": True,
            "allow_irreversible_delete": False,
        }
        cfg["replacement"] = {**cfg["replacement"], "enabled": False}
        cfg["recovery"] = {**cfg["recovery"], "enabled": False}
    return cfg


# ---------------------------------------------------------------------------
# Состояние автопилота
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    """Загружает состояние автопилота из файла.

    Ключи:
      last_run_at (iso str | None) — время последнего запуска
      last_run_window (str | None) — «окно» последнего запуска для крона
      manual_overrides (dict) — {ad_id: until_iso} объявления под защитой
      pending_approvals (dict) — {key: {"ads": [...], "created_at": iso}} ожидают одобрения
      undo_map (dict) — {ad_id: {"ad_id","ad_name","returned","at"}} карта кнопок
        «↩️ Вернуть» отчёта пауз (классика + Live), см. record_pause_undo ниже.
    """
    default = {
        "last_run_at": None,
        "last_run_window": None,
        "manual_overrides": {},
        "pending_approvals": {},
        "undo_map": {},
    }
    if not STATE_FILE.exists():
        return default
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        # Мержим поверх дефолтов, чтобы не падать на отсутствующих ключах
        default.update(data)
        return default
    except Exception as e:
        logger.warning("Не удалось загрузить autopilot_state.json: %s", e)
        return default


def _prune_undo_map(undo_map: dict, now: datetime) -> None:
    """In-place чистка undo_map: удаляет записи старше UNDO_MAP_RETENTION_DAYS
    (по полю 'at', ISO), затем обрезает до MAX_UNDO_ENTRIES самых свежих.

    Зеркало services.auto_launch._prune_stop_map (тот же паттерн ретеншна для
    карты кнопок «Вернуть»/«Остановить» в отчётах). Битую дату не трогает.
    """
    cutoff = now - timedelta(days=UNDO_MAP_RETENTION_DAYS)

    for ad_id in list(undo_map.keys()):
        entry = undo_map.get(ad_id)
        at_raw = entry.get("at") if isinstance(entry, dict) else None
        if not at_raw:
            continue
        try:
            at_dt = datetime.fromisoformat(str(at_raw))
        except ValueError:
            continue
        if at_dt < cutoff:
            del undo_map[ad_id]

    if len(undo_map) <= MAX_UNDO_ENTRIES:
        return

    def _sort_key(item):
        _, entry = item
        at_raw = entry.get("at") if isinstance(entry, dict) else None
        try:
            return datetime.fromisoformat(str(at_raw))
        except (ValueError, TypeError):
            # Записи без валидной даты считаем самыми старыми — уйдут первыми
            return datetime.min.replace(tzinfo=_TZ_LOCAL)

    freshest_ids = {
        ad_id
        for ad_id, _ in sorted(undo_map.items(), key=_sort_key, reverse=True)[:MAX_UNDO_ENTRIES]
    }
    for ad_id in list(undo_map.keys()):
        if ad_id not in freshest_ids:
            del undo_map[ad_id]


def _save_state(state: dict) -> None:
    """Атомарно сохраняет состояние (tmp + rename), чтобы не повредить файл при краше.

    При сохранении чистит протухшие pending_approvals (старше 24 часов) и
    обрезает undo_map по ретеншну (_prune_undo_map).
    """
    # Чистим протухшие записи ожидающих одобрения
    now = datetime.now(_TZ_LOCAL)
    ttl_limit = now - timedelta(hours=24)
    pending = state.get("pending_approvals", {})
    state["pending_approvals"] = {
        key: val for key, val in pending.items()
        if _parse_iso(val.get("created_at")) > ttl_limit
    }

    undo_map = state.get("undo_map", {})
    _prune_undo_map(undo_map, now)
    state["undo_map"] = undo_map

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        logger.error("Не удалось сохранить autopilot_state.json: %s", e)
        raise


def _parse_iso(iso_str: str | None) -> datetime:
    """Парсит ISO-строку в datetime с tzinfo. При ошибке возвращает epoch."""
    if not iso_str:
        return datetime.fromtimestamp(0, tz=_TZ_LOCAL)
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_TZ_LOCAL)
        return dt
    except (ValueError, TypeError):
        return datetime.fromtimestamp(0, tz=_TZ_LOCAL)


def add_manual_override(ad_id: str, days: int = 7) -> None:
    """Добавляет объявление в список «не трогать N дней».

    Вызывается telegram_bot'ом при команде «вернуть объявление».
    Сигнатуру не менять — telegram_bot использует её напрямую.

    Args:
        ad_id: идентификатор объявления FB
        days: сколько дней не трогать (по умолчанию 7)
    """
    state = _load_state()
    until = (datetime.now(_TZ_LOCAL) + timedelta(days=days)).isoformat()
    state["manual_overrides"][ad_id] = until
    _save_state(state)
    logger.info("manual_override: %s защищён до %s", ad_id, until)


def get_active_overrides() -> dict:
    """Возвращает manual_overrides, отфильтровав протухшие записи.

    Returns:
        dict {ad_id: until_iso} — только те, у которых until в будущем
    """
    state = _load_state()
    now_iso = datetime.now(_TZ_LOCAL).isoformat()
    active = {
        ad_id: until
        for ad_id, until in state.get("manual_overrides", {}).items()
        if until > now_iso
    }
    try:
        from agent.database import get_action_state_projections

        for event in get_action_state_projections("UNPAUSE_STATE"):
            until = str(event["payload"].get("override_until") or "")
            if until > now_iso:
                active[str(event["subject_id"])] = until
    except RuntimeError:
        pass
    return active


# ---------------------------------------------------------------------------
# undo_map: карта «ad_id → {ad_name, returned, at}» для кнопки «↩️ Вернуть»
# в отчётах пауз (классика + Live). Паттерн зеркалит stop_map из
# services/auto_launch.py (_record_launch_stop/get_launch_stop_entry/
# mark_launch_stopped), но ключ — ad_id самого объявления (естественный
# уникальный идентификатор, как card_id у stop_map — синтетический короткий
# id не нужен).
# ---------------------------------------------------------------------------

def record_pause_undo(ad_id: str, ad_name: str, now: datetime | None = None) -> None:
    """Регистрирует объявление в undo_map сразу после реальной паузы —
    для идемпотентной обработки кнопки «↩️ Вернуть» в telegram_bot.

    Вызывается из _run_autopilot_inner (классика) и _run_live_inner (Live)
    ПОСЛЕ успешного pause_ad + save_decision + _write_auto_action — то есть
    только когда пауза реально применилась.

    Каждый вызов сбрасывает 'returned' в False: если то же объявление паузится
    повторно (новый прогон после того, как владелец его уже возвращал) — кнопка
    в новом отчёте снова активна.
    """
    now = now or datetime.now(_TZ_LOCAL)
    state = _load_state()
    undo_map = state.setdefault("undo_map", {})
    undo_map[str(ad_id)] = {
        "ad_id": str(ad_id),
        "ad_name": ad_name or str(ad_id),
        "returned": False,
        "at": now.isoformat(),
    }
    _save_state(state)


def get_pause_undo_entry(ad_id: str) -> dict | None:
    """Возвращает копию undo_map[ad_id] или None, если запись не найдена
    (протухла/её не было). Формат: {"ad_id","ad_name","returned","at"}."""
    state = _load_state()
    entry = state.get("undo_map", {}).get(str(ad_id))
    projected = None
    try:
        from agent.database import get_action_state_projections

        pause_events = get_action_state_projections("AUTO_ACTION", str(ad_id))
        for event in pause_events:
            payload = event["payload"]
            if payload.get("action") == "PAUSED" and "undo_returned" in payload:
                projected = {
                    "ad_id": str(ad_id),
                    "ad_name": payload.get("ad_name") or str(ad_id),
                    "returned": bool(payload["undo_returned"]),
                    "at": payload.get("at") or event["created_at"],
                }
        for event in get_action_state_projections("UNPAUSE_STATE", str(ad_id)):
            if projected is not None and event["payload"].get("undo_returned") is True:
                projected["returned"] = True
    except RuntimeError:
        pass
    if projected is not None:
        return projected
    return None if entry is None else dict(entry)


def mark_pause_undone(ad_id: str) -> bool:
    """Помечает undo_map[ad_id]['returned']=True + сохраняет состояние.

    Returns:
        True если запись была и помечена, False если объявления нет в undo_map.
    """
    state = _load_state()
    undo_map = state.get("undo_map", {})
    entry = undo_map.get(str(ad_id))
    if entry is None:
        return False
    entry["returned"] = True
    state["undo_map"] = undo_map
    _save_state(state)
    return True


# ---------------------------------------------------------------------------
# Публичный API для telegram_bot: pending_approvals
# ---------------------------------------------------------------------------

def get_pending_approval(key: str) -> dict | None:
    """Возвращает запись pending_approval по ключу с TTL-проверкой (24 ч).

    Args:
        key: идентификатор прогона (например "run-YYYYMMDDHHMM")

    Returns:
        dict {"ads": [...], "created_at": iso} или None если нет / протухло
    """
    state = _load_state()
    entry = state.get("pending_approvals", {}).get(key)
    if not entry:
        return None
    # TTL-проверка: запись не старше 24 часов
    created = _parse_iso(entry.get("created_at"))
    if datetime.now(_TZ_LOCAL) - created > timedelta(hours=24):
        return None
    result = dict(entry)
    result["ads"] = list(entry.get("ads", []))
    try:
        from agent.database import get_action_state_projections

        applied_ids = {
            str(event["subject_id"])
            for event in get_action_state_projections("AUTO_ACTION")
            if event["payload"].get("pending_key") == key
            and event["payload"].get("action") == "PAUSED"
        }
        result["ads"] = [ad for ad in result["ads"] if str(ad.get("id")) not in applied_ids]
    except RuntimeError:
        pass
    return result


def pop_applied(key: str, ad_ids: list[str]) -> None:
    """Убирает успешно применённые объявления из pending_approvals.

    Если после удаления запись пустая — удаляет её целиком.

    Args:
        key: идентификатор прогона
        ad_ids: список ad_id которые уже применены
    """
    state = _load_state()
    entry = state.get("pending_approvals", {}).get(key)
    if not entry:
        return
    remaining = [ad for ad in entry.get("ads", []) if ad["id"] not in ad_ids]
    if remaining:
        state["pending_approvals"][key]["ads"] = remaining
    else:
        state["pending_approvals"].pop(key, None)
    _save_state(state)


def approve_pause(ad_id: str, meta: dict, command_id: str | None = None) -> tuple[bool, str]:
    """Создаёт PAUSE proposal; legacy Telegram callback не выполняет mutation.

    Args:
        ad_id: идентификатор объявления FB
        meta: dict с полями name, reason, spend, leads, cpl, ctr, romi, qual_pct

    Returns:
        (True, proposal_id) если proposal сохранён
        (False, сообщение об ошибке) при неудаче
    """
    from services.action_producer_gateway import propose_pause
    from services.approval_checker_models import ActionOrigin
    from services.owner_proposal_card import DecisionContext

    ad_name = meta.get("name", ad_id)
    try:
        pause_outcome = propose_pause(
            ad_id,
            origin=ActionOrigin.AUTOPILOT_MANUAL,
            scope=f"autopilot-owner:{command_id or uuid4()}:{ad_id}",
            reason_code="TELEGRAM_PAUSE_INTENT",
            decision=DecisionContext(
                spend_usd=meta.get("spend"),
                leads=meta.get("leads"),
                cpl_usd=meta.get("cpl"),
                qual_pct=meta.get("qual_pct"),
                payments=meta.get("payments"),
                romi_pct=meta.get("romi"),
                business_reason=meta.get("business_reason") or meta.get("reason"),
            ),
        )
        if pause_outcome.receipt is None:
            return False, f"PAUSE proposal не создан: {pause_outcome.reason}"
        meta["_proposal_id"] = pause_outcome.receipt.proposal_id
        logger.info(
            "approve_pause: создан owner proposal %s для %s (%s)",
            pause_outcome.receipt.proposal_id,
            ad_id,
            ad_name,
        )
        return True, pause_outcome.receipt.proposal_id
    except Exception as e:
        logger.error("approve_pause: ошибка паузы %s: %s", ad_id, e)
        return False, str(e)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _write_auto_action(ad_id: str, ad_name: str, action: str, reason: str, confirmed_by: str) -> None:
    """Атомарно дописывает запись в data/auto_actions.json (tmp + rename, как _save_state).

    Совместимо с agent/scheduler.py.
    """
    _AUTO_ACTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    if _AUTO_ACTIONS_FILE.exists():
        try:
            entries = json.loads(_AUTO_ACTIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            entries = []
    entries.insert(0, {
        "timestamp": datetime.now().isoformat(),
        "ad_id": ad_id,
        "ad_name": ad_name,
        "action": action,
        "reason": reason,
        "confirmed_by": confirmed_by,
        "success": True,
    })
    tmp = _AUTO_ACTIONS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries[:500], ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_AUTO_ACTIONS_FILE)


def _enrich_with_amo(ads: list[dict], thresholds: dict) -> tuple[list[dict], bool]:
    """Обогащает объявления AMO-данными (qual_pct, romi, payments, revenue).

    Сначала пробует sync_amo_data (live запрос в AMO).
    При сбое — fallback на amo_repo.get_amo_data (кешированный JSON).
    Если и это пусто/упало → amo_ok=False (паузы будут заблокированы).

    Возвращает:
        (обогащённый список, amo_ok)
    """
    from integrations.amo import sync_amo_data
    from agent.repositories import amo_repo
    from agent.analyzer import apply_decision_tree

    amo_ok = True
    amo_metrics: dict = {}

    # Попытка 1: живые данные из AMO
    try:
        amo_metrics = sync_amo_data(ads, days=30)
    except Exception as e:
        logger.warning("sync_amo_data упал: %s — пробуем fallback", e)
        # Попытка 2: кешированный файл amo_data.json
        try:
            amo_metrics = amo_repo.get_amo_data("default")
            if not amo_metrics:
                logger.warning("amo_repo.get_amo_data вернул пустой словарь — amo_ok=False")
                amo_ok = False
        except Exception as e2:
            logger.error("Fallback amo_repo тоже упал: %s — amo_ok=False", e2)
            amo_ok = False

    # Подставляем AMO-поля в каждое объявление по образцу /api/analytics
    for ad in ads:
        entry = amo_metrics.get(ad["id"], {}) or amo_metrics.get((ad.get("name") or "").lower(), {})
        if entry:
            ad["qual_pct"] = entry.get("qual_pct")
            ad["romi"] = entry.get("romi")
            ad["payments"] = entry.get("payments")
            ad["revenue"] = entry.get("revenue")
            # Гейт: расход = 0 → ROMI невалидны (другой период)
            if float(ad.get("spend", 0) or 0) == 0:
                ad["romi"] = None
            # Гейт: FB-лидов 0 → AMO данные относятся к другому периоду
            if int(ad.get("leads", 0) or 0) == 0:
                ad["qual_pct"] = None
                ad["romi"] = None
                ad["payments"] = None
                ad["revenue"] = None

        # Пересчитываем решение с AMO данными для ACTIVE объявлений
        if ad.get("effective_status") == "ACTIVE":
            decision = apply_decision_tree(ad, thresholds)
            ad["recommendation"] = decision["action"]
            ad["reason"] = decision["reason"]

    return ads, amo_ok


def _enrich_meetings_for_hold(local_ads: list[dict]) -> None:
    """Дополняет local_ads полями meetings_scheduled/meetings_held (in-place).

    ЭКОНОМНО (ARCH-hold-meetings §9): один get_leads(days=30) + count_meetings_by_ad
    на весь прогон (НЕ per-ad) — тот же объём данных, что и так тянет sync_amo_data.
    Матч по fb_ad_id/fb_ad_name как в sync_amo_data (_build_fb_lookup + known_ad_ids
    из ad_id локальной БД).

    Fail-safe: любая ошибка (AMO лёг, таймаут) → у ВСЕХ ads meetings_*=0, лог warning,
    НЕ роняем автопилот и НЕ блокируем паузы — критерий (в) в should_hold деградирует
    до payments/qual (фаза 1).
    """
    # Дефолт на случай сбоя — проставляем сразу, чтобы при исключении на любом
    # шаге ниже local_ads гарантированно имели поля (should_hold читает .get(...,0)).
    for ad in local_ads:
        ad["meetings_scheduled"] = 0
        ad["meetings_held"] = 0

    try:
        from integrations.amo import get_leads, count_meetings_by_ad, _build_fb_lookup

        fb_lookup = _build_fb_lookup(local_ads)
        known_ad_ids = {str(ad["ad_id"]) for ad in local_ads if ad.get("ad_id")}
        leads = get_leads(days=30)
        meetings = count_meetings_by_ad(leads, fb_lookup=fb_lookup, known_ad_ids=known_ad_ids)

        for ad in local_ads:
            ad_meetings = meetings.get(str(ad.get("ad_id")), {})
            ad["meetings_scheduled"] = ad_meetings.get("meetings_scheduled", 0)
            ad["meetings_held"] = ad_meetings.get("meetings_held", 0)
    except Exception as exc:
        logger.warning(
            "_enrich_meetings_for_hold: не удалось добыть встречи из AMO (fail-safe — "
            "meetings=0 для всех, критерий удержания деградирует до payments/qual): %s",
            exc,
        )
        for ad in local_ads:
            ad["meetings_scheduled"] = 0
            ad["meetings_held"] = 0


# ---------------------------------------------------------------------------
# Получение данных для анализа (live → cache fallback)
# ---------------------------------------------------------------------------

def _get_ads_for_analysis(date_from: str, date_to: str, thresholds: dict) -> tuple[list | None, str | dict]:
    """Получает список объявлений для анализа: сначала живые FB, при сбое — кэш.

    Попытка 1: get_ads_with_metrics — живые данные (лучший вариант).
    Попытка 2 (fallback): get_cached_analytics из web.app + проверка свежести
      диск-кэша (saved_at не старше _CACHE_MAX_AGE_HOURS и период ПОКРЫВАЕТ запрошенный:
      cache_from <= date_from И cache_to >= date_to — superset разрешён).

    Superset-кэш (период шире запрошенного) допустим для dry_run;
    при active-режиме паузы всё равно блокируются существующим RAIL (Шаг 6.5).

    Returns:
        (ads, source) — source: "live" | "cache"
        (ads, {"source": "cache", "cache_from": ..., "cache_to": ...}) — superset-кэш
        (None, причина) — если обе попытки не удались
    """
    from agent.analyzer import get_ads_with_metrics

    # Попытка 1: лёгкий live-запрос (без тяжёлых полей creative) — экономит CPU-бюджет FB
    live_err: str | None = None
    try:
        ads = get_ads_with_metrics(date_from=date_from, date_to=date_to, light=True)
        logger.info("_get_ads_for_analysis: получено %d объявлений (live, light=True)", len(ads))
        return ads, "live"
    except Exception as e:
        live_err = str(e)
        logger.warning("get_ads_with_metrics упал (%s) — пробуем кэш", e)

    # Попытка 2: кэш из web.app
    try:
        # Проверяем свежесть диск-кэша: saved_at не старше _CACHE_MAX_AGE_HOURS
        # и период кэша ПОКРЫВАЕТ запрошенный (superset допустим, strict-subset — нет)
        cache_fresh = False
        cache_age_min = None
        cache_from: str | None = None
        cache_to: str | None = None
        if _DISK_CACHE_PATH.exists():
            try:
                disk = json.loads(_DISK_CACHE_PATH.read_text(encoding="utf-8"))
                saved_at = disk.get("saved_at", 0)
                age_sec = time.time() - saved_at
                age_min = int(age_sec / 60)
                cache_age_min = age_min
                cache_from = disk.get("date_from")
                cache_to = disk.get("date_to")
                # Superset-условие: кэш должен ПОКРЫВАТЬ запрошенный период
                period_ok = (
                    cache_from is not None
                    and cache_to is not None
                    and cache_from <= date_from
                    and cache_to >= date_to
                )
                if age_sec <= _CACHE_MAX_AGE_HOURS * 3600 and period_ok:
                    cache_fresh = True
                    logger.info(
                        "_get_ads_for_analysis: диск-кэш свежий (возраст %d мин, кэш %s–%s ⊇ %s–%s)",
                        age_min, cache_from, cache_to, date_from, date_to,
                    )
                else:
                    reason_parts = []
                    if age_sec > _CACHE_MAX_AGE_HOURS * 3600:
                        reason_parts.append(f"возраст {age_min} мин > {_CACHE_MAX_AGE_HOURS * 60} мин")
                    if not period_ok:
                        reason_parts.append(
                            f"период кэша {cache_from}–{cache_to} "
                            f"не покрывает {date_from}–{date_to}"
                        )
                    cache_reason = "кэш устарел: " + "; ".join(reason_parts)
                    logger.warning(
                        "_get_ads_for_analysis: кэш не подходит (%s) — fallback невозможен",
                        "; ".join(reason_parts),
                    )
                    # Возвращаем обе причины: live-ошибка + причина отказа кэша
                    if live_err:
                        return None, f"live: {live_err}; cache: {cache_reason}"
                    return None, cache_reason
            except Exception as ex:
                logger.warning("Ошибка чтения диск-кэша: %s", ex)
                cache_reason = f"ошибка чтения диск-кэша: {ex}"
                if live_err:
                    return None, f"live: {live_err}; cache: {cache_reason}"
                return None, cache_reason
        else:
            cache_reason = "диск-кэш отсутствует"
            if live_err:
                return None, f"live: {live_err}; cache: {cache_reason}"
            return None, cache_reason

        if not cache_fresh:
            cache_reason = "кэш не прошёл проверку свежести"
            if live_err:
                return None, f"live: {live_err}; cache: {cache_reason}"
            return None, cache_reason

        # Получаем данные через get_cached_analytics (in-memory или диск)
        from web.app import get_cached_analytics
        ads = get_cached_analytics(date_from=date_from, date_to=date_to)
        if not ads:
            cache_reason = "get_cached_analytics вернул пустой список"
            if live_err:
                return None, f"live: {live_err}; cache: {cache_reason}"
            return None, cache_reason

        logger.info(
            "_get_ads_for_analysis: fallback на кэш — %d объявлений (возраст %d мин, кэш %s–%s)",
            len(ads), cache_age_min, cache_from, cache_to,
        )
        # Возвращаем метаданные кэша для Telegram-пометки в dry_run
        return ads, {"source": "cache", "cache_from": cache_from, "cache_to": cache_to}

    except Exception as e:
        logger.error("Fallback на кэш тоже упал: %s", e)
        cache_reason = f"кэш недоступен: {e}"
        if live_err:
            return None, f"live: {live_err}; cache: {cache_reason}"
        return None, cache_reason


# ---------------------------------------------------------------------------
# Основная функция автопилота
# ---------------------------------------------------------------------------

def run_autopilot(trigger: str = "cron") -> dict:
    """Запускает один цикл автопилота.

    Args:
        trigger: "cron" (с гейтом по времени) или "manual" (без гейта)

    Returns:
        {
            "ran": bool,
            "skipped_reason": str | None,
            "mode": str,
            "analyzed": int,
            "candidates": [...],
            "paused": [...],
            "errors": [...],
        }
    """
    try:
        return _run_autopilot_inner(trigger)
    except Exception as e:
        logger.exception("Необработанное исключение в run_autopilot: %s", e)
        from services.notifications import send_critical_alert
        try:
            send_critical_alert(
                f"Автопилот: критическая ошибка ({trigger})",
                str(e),
            )
        except Exception:
            pass
        return {"ran": False, "skipped_reason": f"error: {e}", "mode": "unknown",
                "analyzed": 0, "candidates": [], "paused": [], "errors": [str(e)]}


def _run_autopilot_inner(trigger: str) -> dict:
    """Внутренняя реализация run_autopilot (без try/except верхнего уровня)."""
    from agent.scheduler import load_settings
    from agent.repositories import decisions_repo
    from services.notifications import send_telegram, send_critical_alert

    # Шаг 1: читаем конфиг
    cfg = get_autopilot_config()
    if not cfg.get("enabled"):
        logger.info("Автопилот отключён (enabled=False)")
        return {"ran": False, "skipped_reason": "disabled", "mode": cfg["mode"],
                "analyzed": 0, "candidates": [], "paused": [], "errors": []}

    # ГЕЙТ kill_switch — немедленная остановка (зеркало run_autopilot_live, L1100).
    # Без этого «аварийный стоп» не останавливал классический автопилот.
    if cfg.get("kill_switch"):
        logger.info("Автопилот: kill_switch=true — пропускаем")
        return {"ran": False, "skipped_reason": "kill_switch", "mode": cfg["mode"],
                "analyzed": 0, "candidates": [], "paused": [], "errors": []}

    # Шаг 2: гейт по времени (только для cron-запусков)
    state = _load_state()
    if trigger == "cron":
        last_run_at = state.get("last_run_at")
        if last_run_at:
            try:
                last_dt = datetime.fromisoformat(last_run_at)
                min_delta = timedelta(hours=cfg["min_hours_between_runs"])
                now = datetime.now(_TZ_LOCAL)
                # Нормализуем tzinfo для сравнения
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=_TZ_LOCAL)
                if (now - last_dt) < min_delta:
                    reason = f"слишком рано (прошло {(now - last_dt).seconds // 60} мин, нужно {int(cfg['min_hours_between_runs'] * 60)} мин)"
                    logger.info("Автопилот пропускает запуск: %s", reason)
                    return {"ran": False, "skipped_reason": reason, "mode": cfg["mode"],
                            "analyzed": 0, "candidates": [], "paused": [], "errors": []}
            except (ValueError, TypeError) as e:
                logger.warning("Не удалось распарсить last_run_at=%s: %s", last_run_at, e)

    # Шаг 3: получаем метрики (live → cache fallback)
    thresholds = load_settings().get("thresholds") or {}
    now_dt = datetime.now(_TZ_LOCAL)
    date_to = now_dt.strftime("%Y-%m-%d")
    date_from = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d")

    ads, _raw_source = _get_ads_for_analysis(date_from, date_to, thresholds)
    if ads is None:
        logger.error("Не удалось получить данные для анализа: %s", _raw_source)
        # Короткое сообщение о пропуске — чтобы знать что происходит
        try:
            send_telegram(
                f"⚠️ Автопилот ({html.escape(trigger)}): прогон пропущен — "
                f"{html.escape(str(_raw_source))}"
            )
        except Exception as _tg_err:
            logger.warning("Не удалось отправить Telegram при пропуске: %s", _tg_err)
        return {
            "ran": False,
            "skipped_reason": f"нет данных: {_raw_source}",
            "mode": cfg["mode"],
            "analyzed": 0,
            "candidates": [],
            "paused": [],
            "errors": [f"нет данных: {_raw_source}"],
        }

    # Нормализуем источник: _raw_source может быть строкой "live"/"cache"
    # или dict {"source": "cache", "cache_from": ..., "cache_to": ...} (superset-кэш)
    if isinstance(_raw_source, dict):
        data_source = _raw_source["source"]          # всегда "cache"
        cache_period = (_raw_source.get("cache_from"), _raw_source.get("cache_to"))
    else:
        data_source = _raw_source                    # "live" или "cache"
        cache_period = None

    # Шаг 4: обновляем статусы объявлений СВЕЖИМИ данными из FB.
    # Критично при source=="cache": кэш хранит статусы на момент записи,
    # а объявление могло быть поставлено на паузу или удалено вручную с тех пор.
    # При сбое оставляем старые статусы, но запоминаем флаг — active-режим
    # тогда заблокирует паузы (нельзя доверять непроверенным статусам).
    statuses_refreshed = False
    try:
        from agent.analyzer import refresh_statuses_in_place
        refresh_statuses_in_place(ads)
        statuses_refreshed = True
        logger.info("_run_autopilot_inner: статусы объявлений обновлены (refresh_statuses_in_place)")
    except Exception as _refresh_err:
        logger.warning(
            "_run_autopilot_inner: refresh_statuses_in_place упал (%s) — используем статусы из источника",
            _refresh_err,
        )

    # Шаг 4.5: AMO-обогащение + пересчёт Decision Tree
    ads, amo_ok = _enrich_with_amo(ads, thresholds)

    # Шаг 4.6: Портфельный слой поверх абсолютного Decision Tree
    from agent.analyzer import apply_portfolio_decisions
    apply_portfolio_decisions(ads, thresholds)

    # Шаг 5: отбираем кандидатов на паузу.
    # Фильтр effective_status=="ACTIVE" работает по свежим статусам (после refresh),
    # поэтому уже-паузные и удалённые объявления сюда не попадут.
    active_overrides = get_active_overrides()
    candidates = [
        ad for ad in ads
        if ad.get("recommendation") == "ОТКЛЮЧИТЬ"
        and ad.get("effective_status") == "ACTIVE"
        and ad["id"] not in active_overrides
    ]
    # Сортировка по расходу (самые дорогие — первые)
    candidates.sort(key=lambda a: float(a.get("spend", 0) or 0), reverse=True)

    cap = cfg["max_pauses_per_run"]
    to_act = candidates[:cap]

    mode = cfg["mode"]
    paused_ids = []
    errors = []

    # Шаг 6: RAIL — без AMO данных паузы запрещены даже в active-режиме
    if not amo_ok and to_act:
        logger.warning("RAIL: AMO недоступна — блокируем %d паузы", len(to_act))
        names_list = "\n".join(f"• {html.escape(ad['name'])} (${ad.get('spend', 0):.0f})" for ad in to_act)
        send_critical_alert(
            "Автопилот: AMO недоступна — паузы отменены",
            f"Кандидаты на паузу ({len(to_act)}):\n{names_list}",
        )
        _save_state({**state, "last_run_at": datetime.now(_TZ_LOCAL).isoformat()})
        return {
            "ran": True,
            "skipped_reason": "amo_unavailable",
            "mode": mode,
            "source": data_source,
            "statuses_refreshed": statuses_refreshed,
            "analyzed": len(ads),
            "candidates": [_ad_summary(a) for a in candidates],
            "paused": [],
            "errors": ["AMO недоступна — паузы отменены"],
        }

    # Шаг 6.5: RAIL — в active-режиме при данных из кэша паузы запрещены
    # (деньги только по живым данным; кэш — только для dry_run рекомендаций)
    if mode == "active" and data_source == "cache" and to_act:
        logger.warning("RAIL: данные из кэша — блокируем %d паузы в active-режиме", len(to_act))
        names_list = "\n".join(f"• {html.escape(ad['name'])} (${ad.get('spend', 0):.0f})" for ad in to_act)
        send_critical_alert(
            "Автопилот: данные из кэша — паузы отменены (active-режим)",
            f"Кандидаты на паузу ({len(to_act)}):\n{names_list}\n\n"
            f"Паузы будут выполнены при следующем прогоне с живыми данными.",
        )
        _save_state({**state, "last_run_at": datetime.now(_TZ_LOCAL).isoformat()})
        return {
            "ran": True,
            "skipped_reason": "cache_source_active_blocked",
            "mode": mode,
            "source": data_source,
            "statuses_refreshed": statuses_refreshed,
            "analyzed": len(ads),
            "candidates": [_ad_summary(a) for a in candidates],
            "paused": [],
            "errors": ["Данные из кэша — паузы в active-режиме отменены"],
        }

    # Шаг 6.6: RAIL — при сбое обновления статусов в active-режиме паузы запрещены.
    # Без актуальных статусов мы не знаем, не была ли реклама уже поставлена на паузу
    # вручную — слать паузу по непроверенным статусам опасно.
    if mode == "active" and not statuses_refreshed and to_act:
        logger.warning(
            "RAIL: refresh_statuses_in_place не выполнен — блокируем %d паузы в active-режиме",
            len(to_act),
        )
        names_list = "\n".join(f"• {html.escape(ad['name'])} (${ad.get('spend', 0):.0f})" for ad in to_act)
        send_critical_alert(
            "Автопилот: статусы не обновлены — паузы отменены (active-режим)",
            f"Кандидаты на паузу ({len(to_act)}):\n{names_list}\n\n"
            f"Статусы объявлений могут быть устаревшими. Паузы отменены до следующего прогона.",
        )
        _save_state({**state, "last_run_at": datetime.now(_TZ_LOCAL).isoformat()})
        return {
            "ran": True,
            "skipped_reason": "statuses_not_refreshed_active_blocked",
            "mode": mode,
            "source": data_source,
            "statuses_refreshed": False,
            "analyzed": len(ads),
            "candidates": [_ad_summary(a) for a in candidates],
            "paused": [],
            "errors": ["Статусы не обновлены — паузы в active-режиме отменены"],
        }

    # Шаг 7: dry_run — только логируем
    if mode == "dry_run":
        for ad in to_act:
            try:
                decisions_repo.save_decision(
                    "default",
                    ad["id"],
                    ad["name"],
                    "DRY_RUN",
                    ad.get("reason", ""),
                    confirmed_by="autopilot_dry",
                    spend=ad.get("spend"),
                    leads=ad.get("leads"),
                    cpl=ad.get("cpl"),
                    ctr=ad.get("ctr"),
                    romi=ad.get("romi"),
                    qual_pct=ad.get("qual_pct"),
                )
                _write_auto_action(ad["id"], ad["name"], "DRY_RUN", ad.get("reason", ""), "autopilot_dry")
            except Exception as e:
                logger.error("Ошибка записи DRY_RUN для %s: %s", ad["id"], e)
                errors.append(f"DRY_RUN запись {ad['id']}: {e}")

        if to_act:
            # Добавляем пометку об источнике данных в сообщение Telegram
            cache_note = ""
            if data_source == "cache":
                # Формируем пометку: при superset-кэше добавляем период кэша
                try:
                    disk_meta = json.loads(_DISK_CACHE_PATH.read_text(encoding="utf-8"))
                    age_min = int((time.time() - disk_meta.get("saved_at", 0)) / 60)
                    cf = cache_period[0] if cache_period else disk_meta.get("date_from", "?")
                    ct = cache_period[1] if cache_period else disk_meta.get("date_to", "?")
                    cache_note = (
                        f"\n⚠️ <i>(кэш за {html.escape(str(cf))}–{html.escape(str(ct))}, "
                        f"возраст {age_min} мин)</i>"
                    )
                except Exception:
                    cache_note = "\n⚠️ <i>(данные из кэша)</i>"
            lines = "\n".join(
                f"• {html.escape(ad['name'])} — {html.escape(ad.get('reason', ''))} (${ad.get('spend', 0):.0f})"
                for ad in to_act
            )
            msg_text = (
                f"🧪 <b>Автопилот (репетиция)</b>\n"
                f"Отключил бы {len(to_act)} объявлений:\n{lines}"
                f"{cache_note}"
            )

            # Формируем ключ прогона и сохраняем кандидатов для отложенного одобрения
            # Секунды + 4-символьный суффикс исключают коллизию при двух прогонах в одну минуту
            # (например, ручной + крон). Без суффикса второй прогон перезаписал бы pending первого.
            # "applyrun:" = 9 символов, ключ "run-YYYYMMDDHHMMSS-xxxx" = 22 символа → 31 байт, OK (≤ 64)
            run_key = "run-" + datetime.now(_TZ_LOCAL).strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:4]
            pending_ads = [
                {
                    "id": ad["id"],
                    "name": ad.get("name", ""),
                    "spend": ad.get("spend", 0),
                    "reason": ad.get("reason", ""),
                    "leads": ad.get("leads"),
                    "cpl": ad.get("cpl"),
                    "ctr": ad.get("ctr"),
                    "romi": ad.get("romi"),
                    "qual_pct": ad.get("qual_pct"),
                }
                for ad in to_act
            ]

            # Пробуем отправить с inline-кнопками через send_with_buttons
            _sent_with_buttons = False
            try:
                from services.telegram_bot import send_with_buttons as _send_buttons
                # Кнопка «Применить все» — первая строка
                apply_all_label = f"✅ Применить все ({len(to_act)})"
                buttons = [[( apply_all_label, f"applyrun:{run_key}")]]
                # Кнопка на каждое объявление — по одной в ряду
                for ad in to_act:
                    short_name = ad.get("name", "")[:22]
                    cb = f"apply:{ad['id']}"
                    # Проверка: callback_data ≤ 64 байта
                    if len(cb.encode("utf-8")) <= 64:
                        buttons.append([(f"⏸ «{html.escape(short_name)}»", cb)])
                _sent_with_buttons = _send_buttons(msg_text, buttons)
            except ImportError:
                logger.info("send_with_buttons недоступен — fallback на send_telegram")

            # Сохраняем pending только если кнопки успешно отправлены.
            # Иначе пользователь не увидит кнопок, а запись-мусор жила бы 24 часа.
            if _sent_with_buttons:
                state["pending_approvals"] = state.get("pending_approvals") or {}
                state["pending_approvals"][run_key] = {
                    "ads": pending_ads,
                    "created_at": datetime.now(_TZ_LOCAL).isoformat(),
                }
            else:
                send_telegram(msg_text)
        else:
            # Кандидатов нет — отчитываемся кратко
            send_telegram(
                f"✅ Автопилот ({html.escape(trigger)}): проанализировано {len(ads)} объявлений "
                f"— кандидатов на отключение нет"
            )

        _save_state({**state, "last_run_at": datetime.now(_TZ_LOCAL).isoformat()})
        result_dict: dict = {
            "ran": True,
            "skipped_reason": None,
            "mode": mode,
            "source": data_source,
            "statuses_refreshed": statuses_refreshed,
            "analyzed": len(ads),
            "candidates": [_ad_summary(a) for a in candidates],
            "paused": [],
            "errors": errors,
        }
        if cache_period:
            result_dict["cache_period"] = cache_period
        return result_dict

    # Шаг 8: active — реально паузим
    if mode == "active":
        # D3: дневной лимит пауз (по умолчанию 6/день) — без него max_pauses_per_run
        # ограничивал только ОДИН прогон, а прогонов 3/день → до 18 пауз/день фактически.
        max_pauses_per_day = int(cfg.get("max_pauses_per_day", 6))
        classic_daily = _load_classic_daily_state()
        pauses_today = classic_daily.get("pauses_today", 0)
        remaining_today = max_pauses_per_day - pauses_today
        if remaining_today <= 0:
            logger.info(
                "Автопилот (классика): дневной лимит пауз исчерпан (%d/%d) — пропускаем active",
                pauses_today, max_pauses_per_day,
            )
            _save_state({**state, "last_run_at": datetime.now(_TZ_LOCAL).isoformat()})
            return {
                "ran": True,
                "skipped_reason": "daily_cap",
                "mode": mode,
                "source": data_source,
                "statuses_refreshed": statuses_refreshed,
                "analyzed": len(ads),
                "candidates": [_ad_summary(a) for a in candidates],
                "paused": [],
                "errors": [],
            }
        # Ограничиваем прогон остатком дневного лимита (поверх лимита на прогон)
        to_act = to_act[:remaining_today]

        # Предварительный групповой план по полному live inventory. Он сохраняет
        # приоритет и разрешает максимум N-1 пауз из N ACTIVE в одном adset.
        from services.adset_pause_guard import fetch_pause_inventory, plan_safe_pauses
        from services.action_producer_gateway import propose_pause
        from services.approval_checker_models import ActionOrigin
        from services.owner_proposal_card import DecisionContext
        try:
            planned_candidates = list(to_act)
            pause_inventories = fetch_pause_inventory([ad["id"] for ad in to_act])
            allowed_by_guard, blocked_by_guard = plan_safe_pauses(
                to_act, pause_inventories, id_key="id"
            )
            runnable_ids = {str(ad.get("id") or "") for ad in allowed_by_guard}
            to_act = [ad for ad in planned_candidates if str(ad.get("id") or "") in runnable_ids]
        except Exception as exc:
            guard_reason = f"inventory_error:{type(exc).__name__}"
            blocked_by_guard = [
                {**ad, "pause_guard_reason": guard_reason}
                for ad in to_act
            ]
            to_act = []

        if blocked_by_guard:
            blocked_text = "\n".join(
                f"• {ad.get('id')}: {ad.get('pause_guard_reason')}"
                for ad in blocked_by_guard
            )
            _send_critical_alert_non_throwing(
                send_critical_alert,
                "Автопилот: PAUSE заблокированы safety guard",
                blocked_text,
            )
            errors.extend(
                f"pause_guard {ad.get('id')}: {ad.get('pause_guard_reason')}"
                for ad in blocked_by_guard
            )

        action_run_id = str(uuid4())
        proposal_ids: list[str] = []
        for ad in to_act:
            try:
                pause_outcome = propose_pause(
                    ad["id"],
                    origin=ActionOrigin.AUTOPILOT_CLASSIC,
                    scope=f"autopilot-classic:{action_run_id}:{ad['id']}",
                    reason_code="AUTOPILOT_CLASSIC",
                    decision=DecisionContext(
                        spend_usd=ad.get("spend"),
                        leads=ad.get("leads"),
                        cpl_usd=ad.get("cpl"),
                        qual_pct=ad.get("qual_pct"),
                        payments=ad.get("payments"),
                        romi_pct=ad.get("romi"),
                        business_reason=ad.get("business_reason") or ad.get("reason"),
                    ),
                )
                if pause_outcome.receipt is not None:
                    proposal_ids.append(pause_outcome.receipt.proposal_id)
                    logger.info(
                        "Автопилот (классика): proposal %s создан для %s",
                        pause_outcome.receipt.proposal_id,
                        ad["id"],
                    )
                else:
                    err_msg = f"PAUSE proposal {ad['id']} не создан: {pause_outcome.reason}"
                    logger.warning(err_msg)
                    errors.append(err_msg)
                    _send_critical_alert_non_throwing(
                        send_critical_alert,
                        "Автопилот: PAUSE заблокирована safety guard",
                        err_msg,
                    )
                    break
            except Exception as e:
                logger.error("Ошибка паузы %s: %s", ad["id"], e)
                errors.append(f"pause {ad['id']}: {e}")
                continue

        if proposal_ids:
            send_telegram(
                f"📨 Автопилот: владельцу отправлено предложений — {len(proposal_ids)}"
            )
        else:
            # Кандидатов нет — отчитываемся кратко
            send_telegram(
                f"✅ Автопилот ({html.escape(trigger)}): проанализировано {len(ads)} объявлений "
                f"— кандидатов на отключение нет"
            )

        # Провалы пауз (pause_ad вернул False / исключение) — раньше в TG не отражались.
        if errors:
            send_telegram(
                f"⚠️ Автопилот: {len(errors)} пауз НЕ применились — "
                f"{html.escape(str(errors[0]))}. Проверьте права токена/объявления."
            )

        # D3: дневной счётчик классики больше НЕ инкрементируется здесь. Прогон
        # только создаёт предложения (paused_ids всегда пуст), а кап дня считает
        # _load_classic_daily_state по AUTO_ACTION-проекциям подтверждённых
        # эффектов — то есть по фактически исполненным паузам после одобрения.

        # КРИТИЧНО: state в памяти был загружен ДО цикла пауз (см. L809). Внутри
        # цикла record_pause_undo() сам читает/пишет undo_map отдельными
        # _load_state()/_save_state() — то есть на диске undo_map уже свежий.
        # Если сохранить здесь устаревшую in-memory копию state как есть, она
        # затрёт диск своим пустым/старым undo_map, и кнопки «↩️ Вернуть» в
        # отчёте останутся мёртвыми. Поэтому перед финальным сохранением
        # подтягиваем актуальный undo_map с диска.
        state["undo_map"] = _load_state().get("undo_map", {})
        _save_state({**state, "last_run_at": datetime.now(_TZ_LOCAL).isoformat()})
        return {
            "ran": True,
            "skipped_reason": None,
            "mode": mode,
            "source": data_source,
            "statuses_refreshed": statuses_refreshed,
            "analyzed": len(ads),
            "candidates": [_ad_summary(a) for a in candidates],
            "paused": paused_ids,
            "proposals": proposal_ids,
            "errors": errors,
        }

    # Неизвестный режим — логируем и возвращаем
    logger.error("Неизвестный режим автопилота: %s", mode)
    return {
        "ran": False,
        "skipped_reason": f"unknown mode: {mode}",
        "mode": mode,
        "analyzed": 0,
        "candidates": [],
        "paused": [],
        "errors": [f"unknown mode: {mode}"],
    }


def _history_flag(ad_name: str) -> bool:
    """Информационный флаг: есть ли у объявления исторический профиль слабого.

    Проверяет creative_kb: если среди архивных записей с тем же именем
    есть хотя бы одна с creative_class IN ('Dead', 'Clickbait') — возвращает True.
    Не влияет на отбор кандидатов (только информационно).

    Args:
        ad_name: имя объявления (поле name из FB)

    Returns:
        True если одноимённый архивный креатив был Dead или Clickbait
    """
    if not ad_name:
        return False
    try:
        from services.creative_intelligence import DB_PATH
        import sqlite3 as _sqlite3
        if DB_PATH is None:
            return False
        conn = _sqlite3.connect(DB_PATH)
        try:
            row = conn.execute(
                """
                SELECT 1 FROM creative_kb
                WHERE ad_name = ?
                  AND creative_class IN ('Dead', 'Clickbait')
                LIMIT 1
                """,
                (ad_name,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception as exc:
        # creative_kb недоступна (не инициализирована, таблица не создана) — не роняем автопилот
        logger.debug("history_flag недоступен для %s: %s", ad_name, exc)
        return False


# Жёсткий лимит Telegram на длину сообщения (символы, не байты)
_TELEGRAM_MAX_LEN = 4096


def _format_pause_report(paused: list[dict]) -> str:
    """Формирует человекочитаемый Telegram-отчёт о паузах.

    Каждое объявление — отдельный пронумерованный блок с пустой строкой
    между блоками: город жирным, расход/лиды/CPL, квал+оплаты (честно "—"
    если данных нет — раньше квал вообще не показывался), причина паузы
    человеческой фразой в конце блока.

    Если итоговый текст не влезает в лимит Telegram (4096 символов) —
    обрезаем по целым блокам и добавляем "…и ещё N (см. дашборд)".

    Текстового футера «Вернуть любую: дашборд → История решений» больше нет —
    его заменили inline-кнопки «↩️ Вернуть» (см. _build_pause_undo_buttons,
    прикрепляются отдельно через _send_pause_report/send_with_buttons).
    """
    header = (
        f"🤖 <b>Автопилот: предлагаю паузу {len(paused)} объявлений</b> — жду решения\n"
        "⏸ Ничего не выключено: рекламы работают, это только предложения. "
        "Кнопки — в карточках/дайджесте (/digest)."
    )

    blocks = [_format_pause_block(i, ad) for i, ad in enumerate(paused, start=1)]

    # Пытаемся уместить все блоки. Если не влезает — усекаем по одному блоку
    # с конца, пока не влезет (+ строка "…и ещё N").
    for cut in range(len(blocks), 0, -1):
        included = blocks[:cut]
        omitted = len(blocks) - cut
        parts = [header, "", "\n\n".join(included)]
        if omitted:
            parts.append(f"\n…и ещё {omitted} (см. дашборд)")
        text = "\n".join(parts)
        if len(text) <= _TELEGRAM_MAX_LEN:
            return text

    # Даже один блок не влезает (аномально длинное имя) — режем текст блока грубо.
    parts = [header, "", blocks[0][: _TELEGRAM_MAX_LEN // 2]]
    if len(paused) > 1:
        parts.append(f"…и ещё {len(paused) - 1} (см. дашборд)")
    return "\n".join(parts)


def _build_pause_undo_buttons(paused: list[dict]) -> list[list[tuple[str, str]]]:
    """Строит кнопки «↩️ Вернуть <короткое имя>» для отчёта пауз — по одной на
    объявление, максимум MAX_PAUSE_UNDO_BUTTONS (владелец против простыни
    кнопок при массовой паузе). Регистрация в undo_map делается ОТДЕЛЬНО, в
    момент реальной паузы (record_pause_undo) — здесь только рендер кнопок из
    уже запаузенного списка (чистая функция форматирования, без побочных
    эффектов, как _build_active_report в services/auto_launch.py).

    callback_data = "undo_pause:<ad_id>" — ad_id уже валиден (цифровой FB id
    ≤25 символов), с префиксом гарантированно ≤64 байта, но проверка оставлена
    как страховка (мирроит stop_launch).
    """
    buttons: list[list[tuple[str, str]]] = []
    for ad in paused[:MAX_PAUSE_UNDO_BUTTONS]:
        ad_id = ad.get("id")
        if not ad_id:
            continue
        callback_data = f"undo_pause:{ad_id}"
        if len(callback_data.encode("utf-8")) > 64:
            continue
        short_name = truncate_at_word_boundary(str(ad.get("name") or ad_id), 22)
        buttons.append([(f"↩️ Вернуть «{short_name}»", callback_data)])
    return buttons


def _send_pause_report(paused_ads: list[dict]) -> None:
    """Отправляет ранговый отчёт о ПРЕДЛОЖЕННЫХ паузах без кнопок.

    Кнопок «↩️ Вернуть» здесь больше нет: отчёт перечисляет предложения,
    ничего не выключено — возвращать нечего (решение владельца). Решения
    принимаются на карточках предложений (Одобрить/Отклонить/Отложить)
    и в дайджесте /digest.
    """
    from services.notifications import send_telegram

    send_telegram(_format_pause_report(paused_ads))


def _format_pause_block(index: int, ad: dict) -> str:
    """Форматирует один блок отчёта об отключённом объявлении."""
    name = ad.get("name", "") or ""
    if len(name) > 60:
        name = name[:57] + "..."
    city = ad.get("city") or "—"

    spend = ad.get("spend")
    spend_str = fmt_money(spend, "$")
    leads = ad.get("leads")
    leads_str = f"{leads} лид{_lead_suffix(leads)}" if leads is not None else "лидов —"
    cpl = ad.get("cpl")
    cpl_str = fmt_money(cpl, "$") if cpl is not None else "—"

    qual_pct = ad.get("qual_pct")
    leads_n = leads or 0
    if qual_pct is not None and leads_n:
        qual_n = round(qual_pct / 100 * leads_n)
        qual_str = f"квал {qual_n} ({qual_pct:.0f}%)"
    elif qual_pct is not None:
        qual_str = f"квал {qual_pct:.0f}%"
    else:
        qual_str = "квал —"

    payments = ad.get("payments")
    payments_str = f"оплат {payments}" if payments is not None else "оплат —"

    reason = ad.get("reason", "") or "без причины"

    return (
        f"{index}. <b>{html.escape(city)}</b> | {html.escape(name)}\n"
        f"   💸 {spend_str} · {leads_str} · CPL {cpl_str}\n"
        f"   👥 {qual_str} · {payments_str}\n"
        f"   📉 {html.escape(reason)}"
    )


def _lead_suffix(leads: int) -> str:
    """Русское окончание для «лид/лида/лидов» по числу (для отчёта пауз)."""
    if leads % 10 == 1 and leads % 100 != 11:
        return ""
    if 2 <= leads % 10 <= 4 and not (12 <= leads % 100 <= 14):
        return "а"
    return "ов"


def _ad_summary(ad: dict) -> dict:
    """Краткая сводка по объявлению для результата run_autopilot."""
    return {
        "id": ad["id"],
        "name": ad.get("name", ""),
        "reason": ad.get("reason", ""),
        "spend": ad.get("spend", 0),
        "leads": ad.get("leads", 0),
        "romi": ad.get("romi"),
        "qual_pct": ad.get("qual_pct"),
        "history_flag": _history_flag(ad.get("name", "")),
    }


def _count_confirmed_pauses_today(today: str) -> int:
    """Исполненные паузы дня по конвейеру предложений (owner_action_attempts).

    Единственный источник правды об исполненной паузе — попытка
    PAUSE_AD в состоянии CONFIRMED. День — по локальному времени: границы суток переводим
    в UTC, в котором хранится completed_at. Любая ошибка → 0 (fail-open к
    файловому счётчику, как раньше).
    """
    try:
        from services.creative_intelligence import _get_connection

        day_start = datetime.fromisoformat(today).replace(tzinfo=_TZ_LOCAL)
        day_end = day_start + timedelta(days=1)
        conn = _get_connection()
        try:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT resource_id) FROM owner_action_attempts
                WHERE operation_kind = 'PAUSE_AD' AND state = 'CONFIRMED'
                  AND completed_at >= ? AND completed_at < ?
                """,
                (
                    day_start.astimezone(timezone.utc).isoformat(),
                    day_end.astimezone(timezone.utc).isoformat(),
                ),
            ).fetchone()
        finally:
            conn.close()
        return int(row[0] or 0)
    except Exception as exc:  # noqa: BLE001 — БД не готова / таблицы нет
        logger.debug("_count_confirmed_pauses_today: %s", exc)
        return 0


def _projected_pause_count(counter_scope: str, today: str) -> int:
    """Считает подтверждённые паузы дня: исполненные попытки конвейера + legacy-проекции.

    Раньше читались только AUTO_ACTION-проекции с counter_scope, которые
    никто не писал (единственный писатель — agent/scheduler.py, без scope), и
    дневной лимит max_pauses_per_day фактически видел 0. Теперь основа —
    owner_action_attempts (обе шкалы, live и classic, исполняются одним
    конвейером, поэтому scope на счёт не влияет), legacy-проекции добавляются
    для совместимости со старым планировщиком.
    """
    count = _count_confirmed_pauses_today(today)
    try:
        from agent.database import get_action_state_projections

        for event in get_action_state_projections("AUTO_ACTION"):
            payload = event["payload"]
            if payload.get("action") != "PAUSED":
                continue
            if payload.get("counter_scope") != counter_scope:
                continue
            if _parse_iso(payload.get("at")).astimezone(_TZ_LOCAL).date().isoformat() == today:
                count += 1
    except RuntimeError:
        # До init_db остаётся совместимость с файловым счётчиком.
        pass
    return count


# ---------------------------------------------------------------------------
# Дневной счётчик пауз для run_autopilot_live
# ---------------------------------------------------------------------------

def _load_live_daily_state() -> dict:
    """Загружает дневной счётчик пауз из файла.

    Структура: {"date": "YYYY-MM-DD", "pauses_today": int}
    При смене даты счётчик сбрасывается в 0.
    """
    today = datetime.now(_TZ_LOCAL).date().isoformat()
    projected_count = _projected_pause_count("live", today)
    default = {"date": today, "pauses_today": projected_count}
    if not _LIVE_DAILY_STATE_FILE.exists():
        return default
    try:
        data = json.loads(_LIVE_DAILY_STATE_FILE.read_text(encoding="utf-8"))
        # Если дата изменилась — сбрасываем счётчик
        if data.get("date") != today:
            return default
        file_count = int(data.get("pauses_today", 0))
        return {
            "date": today,
            "pauses_today": max(file_count, projected_count),
        }
    except Exception as e:
        logger.warning("Не удалось загрузить autopilot_live_daily.json: %s", e)
        return default


def _save_live_daily_state(state: dict) -> None:
    """Атомарно сохраняет дневной счётчик пауз."""
    _LIVE_DAILY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _LIVE_DAILY_STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_LIVE_DAILY_STATE_FILE)
    except Exception as e:
        logger.error("Не удалось сохранить autopilot_live_daily.json: %s", e)
        raise


# ---------------------------------------------------------------------------
# Дневной счётчик пауз для классического автопилота (_run_autopilot_inner, D3).
#
# Раньше лимит max_pauses_per_run действовал только НА ОДИН ПРОГОН, а прогонов
# в сутки 3 (крон) → фактически до 18 пауз/день вместо ожидаемых 6. Дневной
# счётчик здесь — отдельный state-файл (не LIVE), потому что классический и
# LIVE автопилоты паузят независимо друг от друга, у каждого свой кап 6/день.
# ---------------------------------------------------------------------------

def _load_classic_daily_state() -> dict:
    """Загружает дневной счётчик пауз классического автопилота.

    Структура: {"date": "YYYY-MM-DD", "pauses_today": int}
    При смене даты счётчик сбрасывается в 0.
    """
    today = datetime.now(_TZ_LOCAL).date().isoformat()
    projected_count = _projected_pause_count("classic", today)
    default = {"date": today, "pauses_today": projected_count}
    if not _CLASSIC_DAILY_STATE_FILE.exists():
        return default
    try:
        data = json.loads(_CLASSIC_DAILY_STATE_FILE.read_text(encoding="utf-8"))
        # Если дата изменилась — сбрасываем счётчик
        if data.get("date") != today:
            return default
        file_count = int(data.get("pauses_today", 0))
        return {
            "date": today,
            "pauses_today": max(file_count, projected_count),
        }
    except Exception as e:
        logger.warning("Не удалось загрузить autopilot_classic_daily.json: %s", e)
        return default


def _save_classic_daily_state(state: dict) -> None:
    """Атомарно сохраняет дневной счётчик пауз классического автопилота."""
    _CLASSIC_DAILY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CLASSIC_DAILY_STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CLASSIC_DAILY_STATE_FILE)
    except Exception as e:
        logger.error("Не удалось сохранить autopilot_classic_daily.json: %s", e)
        raise


# ---------------------------------------------------------------------------
# Боевой автопилот: run_autopilot_live
# ---------------------------------------------------------------------------

_LIVE_LEADS_BATCH_SIZE = 50


def _parse_fb_lifetime_leads_row(row: dict) -> int | None:
    """Возвращает безопасный lifetime lead total либо None при неоднозначности."""
    from services.meta_lead_actions import parse_meta_lead_actions

    if not isinstance(row, dict):
        return None
    result = parse_meta_lead_actions(row.get("actions"))
    if result.status != "ok":
        return None
    return result.canonical_total


def _evidence_accounts() -> tuple[str, ...]:
    """Кабинеты для lifetime-evidence: все оффлайн из карты роутинга.

    Один кабинет здесь — это фабрикация нулей для второго: «нет строки в
    инсайтах cabinet_a = 0 лидов» проставляла бы живым cabinet_b-объявлениям
    подтверждённый ноль (известный сценарий: account-scoped запрос паузит
    живую рекламу второго кабинета). Фолбэк — дефолтный кабинет.
    """
    try:
        from services.launch_routing import accounts_to_scan

        accounts = accounts_to_scan()
        if accounts:
            return accounts
    except Exception as exc:  # noqa: BLE001 — деградация до старого охвата
        logger.warning("lifetime lead evidence: карта роутинга недоступна — %s", exc)
    from services.fb_token_provider import get_fb_account_id

    return (str(get_fb_account_id()).replace("act_", ""),)


def _fetch_live_lifetime_lead_evidence(ad_ids: list[str]) -> dict[str, int | None]:
    """Возвращает атомарное complete lifetime-leads evidence по batch до 50 ads.

    Мультикабинетно: лиды объявления суммируются по всем оффлайн-кабинетам
    карты роутинга. «0 лидов» публикуется только если НИ один кабинет не
    отказал по батчу с этим ad_id — иначе evidence остаётся unknown (None),
    и правило нулевых лидов fail-closed блокируется.
    """
    if not ad_ids:
        return {}

    from agent.fb_common import API, _throttled_get
    from services.fb_token_provider import (
        fb_account,
        get_fb_account_id,
        get_fb_token,
        offline_account_context,
    )

    requested_ids = list(dict.fromkeys(str(ad_id) for ad_id in ad_ids))
    result: dict[str, int | None] = {ad_id: None for ad_id in requested_ids}
    counted: dict[str, int] = {}
    failed_ids: set[str] = set()

    for account_id in _evidence_accounts():
        try:
            account_ctx = offline_account_context(account_id)
        except Exception as exc:  # noqa: BLE001 — незарегистрированный кабинет
            logger.warning(
                "lifetime lead evidence: кабинет %s отвергнут (%s) — все ads unknown",
                account_id,
                exc,
            )
            failed_ids.update(requested_ids)
            continue
        with fb_account(account_ctx):
            for start in range(0, len(requested_ids), _LIVE_LEADS_BATCH_SIZE):
                batch = requested_ids[start:start + _LIVE_LEADS_BATCH_SIZE]
                batch_set = set(batch)
                try:
                    response = _throttled_get(
                        f"{API}/act_{get_fb_account_id()}/insights",
                        params={
                            "access_token": get_fb_token(),
                            "level": "ad",
                            "date_preset": "maximum",
                            "fields": "ad_id,actions",
                            "filtering": json.dumps([
                                {"field": "ad.id", "operator": "IN", "value": batch}
                            ]),
                            "limit": len(batch),
                        },
                    )
                    if response.status_code != 200:
                        raise ValueError(f"http_{response.status_code}")

                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                        raise ValueError("malformed_payload")
                    paging = payload.get("paging")
                    if paging is not None and (not isinstance(paging, dict) or paging.get("next")):
                        raise ValueError("incomplete_paging")

                    parsed_rows: dict[str, int] = {}
                    for row in payload["data"]:
                        if not isinstance(row, dict):
                            raise ValueError("malformed_row")
                        ad_id = row.get("ad_id")
                        if not isinstance(ad_id, str) or ad_id not in batch_set:
                            raise ValueError("unrequested_ad_id")
                        if ad_id in parsed_rows:
                            raise ValueError("duplicate_ad_id")
                        leads = _parse_fb_lifetime_leads_row(row)
                        if leads is None:
                            raise ValueError("invalid_lead_actions")
                        parsed_rows[ad_id] = leads

                    # Учитываем строки кабинета; отсутствие строки = 0 в ЭТОМ
                    # кабинете (законно: объявление живёт в другом кабинете).
                    for ad_id, leads in parsed_rows.items():
                        counted[ad_id] = counted.get(ad_id, 0) + leads
                except Exception as exc:
                    logger.warning(
                        "lifetime lead evidence: act_%s batch из %d ads не собран (%s)",
                        account_id,
                        len(batch),
                        type(exc).__name__,
                    )
                    failed_ids.update(batch)

    # Публикуем только полностью подтверждённое: ноль — лишь когда все кабинеты
    # ответили по батчам с этим ad_id.
    for ad_id in requested_ids:
        if ad_id in failed_ids:
            continue
        result[ad_id] = counted.get(ad_id, 0)

    return result


def _refresh_live_zero_lead_evidence(local_ads: list[dict]) -> dict:
    """Перепроверяет только локальные zero-lead ads возраста 3+ через live FB."""
    from services.decision_policy import (
        _known_age_days,
        _parse_nonnegative_int,
    )

    candidates: list[dict] = []
    requested: list[str] = []
    for ad in local_ads:
        age_days = _known_age_days(ad)
        local_leads = _parse_nonnegative_int(ad.get("leads"))
        if age_days is None or age_days < 3 or local_leads != 0:
            continue
        candidates.append(ad)
        ad_id = ad.get("ad_id")
        if isinstance(ad_id, str) and ad_id and ad_id not in requested:
            requested.append(ad_id)

    if not candidates:
        return {"requested": [], "verified_zero": [], "nonzero": [], "unknown": []}

    try:
        evidence = _fetch_live_lifetime_lead_evidence(requested)
    except Exception as exc:
        logger.warning(
            "lifetime lead evidence: проверка local-zero ads не выполнена (%s)",
            type(exc).__name__,
        )
        evidence = {ad_id: None for ad_id in requested}

    verified_zero: list[str] = []
    nonzero: list[str] = []
    unknown: list[str] = []
    for ad in candidates:
        ad_id = ad.get("ad_id")
        exact_leads = evidence.get(ad_id) if isinstance(ad_id, str) else None
        ad["leads"] = exact_leads
        if exact_leads is None:
            if isinstance(ad_id, str) and ad_id and ad_id not in unknown:
                unknown.append(ad_id)
        elif exact_leads == 0:
            if ad_id not in verified_zero:
                verified_zero.append(ad_id)
        elif ad_id not in nonzero:
            nonzero.append(ad_id)

    return {
        "requested": requested,
        "verified_zero": verified_zero,
        "nonzero": nonzero,
        "unknown": unknown,
    }


def _fetch_candidate_fb_info(ad_ids: list[str]) -> dict:
    """Запрашивает у FB adset_id + effective_status только по переданным ad_id.

    Использует batch-подход: по 50 id за запрос.
    Возвращает {ad_id: {"adset_id": str, "effective_status": str}}.
    При ошибке FB возвращает пустой словарь — вызывающий должен это учесть.
    """
    if not ad_ids:
        return {}

    from agent.fb_common import _throttled_get, API
    from services.fb_token_provider import get_fb_token

    result: dict = {}
    # Делим на батчи по 50 id (лёгкий запрос, но не стоит загружать в один URL)
    batch_size = 50
    for i in range(0, len(ad_ids), batch_size):
        chunk = ad_ids[i: i + batch_size]
        ids_param = ",".join(chunk)
        try:
            resp = _throttled_get(
                f"{API}",
                params={
                    "access_token": get_fb_token(),
                    "ids": ids_param,
                    "fields": "id,adset_id,effective_status",
                },
            )
            if resp.status_code != 200:
                logger.warning(
                    "_fetch_candidate_fb_info: FB вернул %d для %d id",
                    resp.status_code, len(chunk),
                )
                continue
            data = resp.json()
            for ad_id, info in data.items():
                result[ad_id] = {
                    "adset_id": info.get("adset_id", ""),
                    "effective_status": info.get("effective_status", "UNKNOWN"),
                }
        except Exception as exc:
            logger.warning("_fetch_candidate_fb_info: ошибка для chunk[%d]: %s", i, exc)
    return result


def run_autopilot_live(max_pauses: int = 8, trigger: str = "manual") -> dict:
    """Боевой автопилот — ставит относительных аутсайдеров на паузу.

    Отличия от run_autopilot (dry_run/active):
    - Данные только из локальной БД (shadow_report._fetch_ads_from_local_db),
      без живого FB-запроса метрик — быстро, < 1 секунды.
    - Рейтинг через decision_policy.score_and_decide (относительный).
    - Свежая FB-проверка ТОЛЬКО по кандидатам (не по всем 176+).
    - Guardrail «не пустеть»: adset обязан сохранять ≥1 ACTIVE после паузы.
    - kill_switch — немедленная остановка без алертов.
    - Telegram-отчёт с кнопкой «Вернуть» (undo_pause:<ad_id>) на каждую паузу.

    Args:
        max_pauses: максимум паузы за один прогон (дефолт из конфига).
        trigger: "manual" или "cron" (для логирования).

    Returns:
        {
            "ran": bool,
            "skipped": str | None,  # причина пропуска
            "analyzed": int,        # объявлений в локальной БД
            "candidates": [...],    # отобранных до guardrail
            "paused": [...],        # реально запаузено
            "errors": [...],        # ошибки по отдельным объявлениям
        }
    """
    try:
        return _run_live_inner(max_pauses=max_pauses, trigger=trigger)
    except Exception as exc:
        logger.exception("run_autopilot_live: критическая ошибка (%s): %s", trigger, exc)
        # Алерт, но без паузы — безопасный fallback
        try:
            from services.notifications import send_critical_alert
            send_critical_alert(
                f"Автопилот Live: критическая ошибка ({trigger})",
                str(exc),
            )
        except Exception:
            pass
        return {
            "ran": False,
            "skipped": f"error: {exc}",
            "analyzed": 0,
            "candidates": [],
            "paused": [],
            "errors": [str(exc)],
        }


def _run_live_inner(max_pauses: int, trigger: str) -> dict:
    """Внутренняя реализация run_autopilot_live (без try/except верхнего уровня)."""
    from services.notifications import send_telegram, send_critical_alert

    # Шаг 1: читаем конфиг
    cfg = get_autopilot_config()

    # ГЕЙТ 1: enabled
    if not cfg.get("enabled"):
        logger.info("run_autopilot_live: autopilot.enabled=false — пропускаем")
        return {"ran": False, "skipped": "disabled", "analyzed": 0,
                "candidates": [], "paused": [], "errors": []}

    # ГЕЙТ 2: kill_switch
    if cfg.get("kill_switch"):
        logger.info("run_autopilot_live: kill_switch=true — пропускаем")
        return {"ran": False, "skipped": "kill_switch", "analyzed": 0,
                "candidates": [], "paused": [], "errors": []}

    # Берём max_pauses из конфига, но не превышаем переданный параметр
    cfg_cap = int(cfg.get("max_pauses_per_run", 8))
    cap = min(max_pauses, cfg_cap)

    # ГЕЙТ 3: дневной лимит пауз
    # Считаем сколько пауз осталось в сегодняшней квоте
    max_pauses_per_day = int(cfg.get("max_pauses_per_day", 6))
    daily_state = _load_live_daily_state()
    pauses_today = daily_state.get("pauses_today", 0)
    remaining_today = max_pauses_per_day - pauses_today
    if remaining_today <= 0:
        logger.info(
            "run_autopilot_live: дневной лимит пауз исчерпан (%d/%d) — пропускаем",
            pauses_today, max_pauses_per_day,
        )
        return {"ran": False, "skipped": "daily_cap", "analyzed": 0,
                "candidates": [], "paused": [], "errors": []}
    # Ограничиваем текущий прогон остатком дневного лимита
    cap = min(cap, remaining_today)

    # Минимальное число дней с момента запуска — не трогаем «молодые» объявления
    min_days_protect = int(cfg.get("min_days_protect", 5))

    # Шаг 2: локальный сбор активных объявлений
    try:
        from services.shadow_report import _fetch_ads_from_local_db
        local_ads = _fetch_ads_from_local_db()
    except Exception as exc:
        logger.error("run_autopilot_live: _fetch_ads_from_local_db упал: %s", exc)
        send_critical_alert("Автопилот Live: ошибка сбора данных из БД", str(exc))
        return {
            "ran": False,
            "skipped": f"db_error: {exc}",
            "analyzed": 0,
            "candidates": [],
            "paused": [],
            "errors": [f"db_error: {exc}"],
        }

    if not local_ads:
        logger.info("run_autopilot_live: нет активных объявлений в локальной БД")
        send_telegram("✅ Автопилот Live: нет активных объявлений в базе")
        return {"ran": True, "skipped": None, "analyzed": 0,
                "candidates": [], "paused": [], "errors": []}

    # Локальный 0 — только повод запросить evidence, но не доказательство для PAUSE.
    # Unknown/неполный FB-ответ превращает leads в None и fail-closed блокирует правило.
    _refresh_live_zero_lead_evidence(local_ads)

    # Шаг 3: score_and_decide → кандидаты PAUSE
    try:
        from services.decision_policy import score_and_decide
        from agent.scheduler import load_settings
        thresholds = load_settings().get("thresholds") or {}
        # Страж: домешиваем autopilot.guardian в thresholds, чтобы правила
        # early_waster/wasted_no_crm внутри score_and_decide видели актуальные
        # пороги/dry_run-флаги владельца (см. ARCH-phase1-guardian.md §6.2, §6.4).
        # cfg уже содержит блок "guardian" (мерж AUTOPILOT_DEFAULTS + settings.json).
        thresholds = {**thresholds, **cfg.get("guardian", {})}
        # Тренд недельных когорт (волна 3): читаем ОДИН раз на прогон здесь и
        # передаём в чистое правило — decision_policy остаётся без I/O.
        # load_trend_context никогда не бросает: сбой чтения когорт даёт пустой
        # контекст, и решения получаются ровно такими же, как до волны 3.
        trend_ctx = trend_gate.load_trend_context(cfg=cfg)
        if trend_ctx.error:
            logger.warning(
                "run_autopilot_live: тренд недоступен (%s) — решения без тренда",
                trend_ctx.error,
            )
        decisions = score_and_decide(local_ads, thresholds, trend_ctx=trend_ctx)
        _log_trend_effect(decisions, trend_ctx)
    except Exception as exc:
        logger.error("run_autopilot_live: score_and_decide упал: %s", exc)
        send_critical_alert("Автопилот Live: ошибка decision_policy", str(exc))
        return {
            "ran": False,
            "skipped": f"decision_error: {exc}",
            "analyzed": len(local_ads),
            "candidates": [],
            "paused": [],
            "errors": [f"decision_error: {exc}"],
        }

    # Маппинг ad_id → оригинальный ад из локальной БД (для метрик)
    local_by_id: dict[str, dict] = {
        ad["ad_id"]: ad for ad in local_ads
    }

    # Шаг 3.5: истечение удержания (ARCH-rank-pause-hold §8) — выполняется ЗДЕСЬ,
    # ДО ранних return по пустым pause_decisions/final_list, иначе survived-записи
    # никогда не очистятся, а истёкшие-без-роста-оплат зависнут в hold-стейте
    # навсегда (найдено интеграционным тестом T5). "survived" не требует FB-проверки
    # (просто сброс записи). "pause" требует подтверждения ACTIVE из FB — такие
    # ad_id добавляются в expired_pause_entries и идут в тот же Шаг 4 (свежая
    # FB-проверка) и Шаг 5 (guardrail), что и обычные ранговые кандидаты.
    expired_pause_entries: dict[str, dict] = {}  # ad_id -> HoldEntry (для reason/ad_name)
    # Реклама, которая на момент прогона была под удержанием (снимок hold-стейта
    # ДО его изменений этим прогоном). Удержание — решение «дать шанс, не паузить
    # сейчас», и автономия его не отменяет: такая реклама в этом прогоне
    # автоматически не выключается, даже если удержание тут же закрылось
    # («пережито» / истекло). Пауза по ней, если она нужна, уедет владельцу
    # карточкой с кнопкой — см. Шаг 5.7.
    held_this_run: set[str] = set()
    # False = достоверного hold-стейта в этом прогоне нет (блок удержания упал).
    # Тогда бот не знает, кто под удержанием, и не имеет права выключать сам —
    # предложения владельцу при этом идут как раньше, деньги не теряются.
    hold_state_trusted = True
    if cfg.get("hold_enabled"):
        try:
            from services.autopilot_hold import load_hold_state, save_hold_state, check_hold_expired

            hold_state = load_hold_state()
            holds = hold_state.get("holds", {})
            held_this_run |= {str(ad_id) for ad_id in holds}
            now = datetime.now(_TZ_LOCAL)
            expired_ids = [
                ad_id for ad_id, entry in holds.items()
                if now >= _parse_iso(entry.get("hold_until"))
            ]
            for ad_id in expired_ids:
                entry = holds[ad_id]
                ad_metrics = local_by_id.get(ad_id, {})
                verdict, expire_reason = check_hold_expired(ad_id, ad_metrics, hold_state)
                if verdict == "survived":
                    logger.info(
                        "run_autopilot_live: удержание пережито %s (%s) — оплаты выросли, сброс",
                        ad_id, entry.get("ad_name", ""),
                    )
                    del holds[ad_id]
                else:
                    # Запись НЕ удаляем сразу — удалим после FB-подтверждения ниже
                    # (если объявление не ACTIVE/не найдено в FB — оставляем в hold,
                    # следующий прогон попробует снова, деньги важнее).
                    entry["_expire_reason"] = expire_reason
                    expired_pause_entries[ad_id] = entry

            hold_state["holds"] = holds
            save_hold_state(hold_state)
        except Exception as exc:
            logger.error(
                "run_autopilot_live: ошибка обработки истечения удержания (fail-safe — пропускаем): %s",
                exc,
            )
            send_critical_alert(
                "Автопилот Live: ошибка истечения удержания",
                f"Обработка истечения пропущена в этом прогоне: {exc}",
            )
            expired_pause_entries = {}
            hold_state_trusted = False

    # Отбираем только PAUSE, исключаем manual_overrides и «молодые» объявления.
    # confirmed_waster (тир A/B) пропускает защиту по min_days_protect:
    # объявление с $300+ расхода и 0 оплатами — слив независимо от days_running в БД,
    # который может быть = 0 из-за перезаписи sync_knowledge_base без поля days_running.
    # Кандидатов с истёкшим удержанием (expired_pause_entries) исключаем ЗДЕСЬ —
    # они уже помечены на паузу отдельным потоком (Шаг 3.5), обычная hold-логика
    # (should_hold/is_held ниже) их трогать не должна — иначе pause_ad вызовется дважды.
    from services.decision_policy import _known_age_days

    def _age_protect_passed(decision: dict) -> bool:
        """Возрастная защита score-кандидатов, fail-closed по возрасту.

        Раньше стояло ``days_running`` с дефолтом 999: у свежесозданных
        объявлений поля в KB ещё нет, и «неизвестный возраст» превращался в
        «очень старое» — карточки предлагали паузить рекламы, запущенные
        накануне (сутки открутки). Неизвестный возраст теперь означает
        «не кандидат», а не «кандидат».
        """
        age = _known_age_days(local_by_id.get(decision["ad_id"], {}))
        return age is not None and age >= min_days_protect

    active_overrides = get_active_overrides()
    pause_decisions = [
        d for d in decisions
        if d["action"] == "PAUSE"
        and d["ad_id"] not in active_overrides
        and d["ad_id"] not in expired_pause_entries
        and (
            d.get("is_zero_leads_after_3d")
            or d.get("is_confirmed_waster")  # сливы не защищаем по возрасту
            or _age_protect_passed(d)
        )
    ]

    # Дополнительные генераторы кандидатов (ручной разбор вскрыл классы,
    # невидимые score_and_decide): семья-слив (один креатив льёт в
    # нескольких городах, каждый город под порогами R1) и «дорогой без выручки»
    # (квалы есть, продаж за 14-дневный цикл оплаты — ноль). Оба правила
    # fail-closed внутри и live-считают AMO; сбой любого — лог, не прогон.
    try:
        from services.family_waster import find_family_wasters
        from services.no_revenue_waster import find_no_revenue_wasters

        _extra_known = {d["ad_id"] for d in pause_decisions}
        _extra_generators = (
            ("family_waster", find_family_wasters),
            ("no_revenue_waster", find_no_revenue_wasters),
        )
        for _gen_name, _generator in _extra_generators:
            try:
                for _extra in _generator(local_ads):
                    _extra_id = _extra["ad_id"]
                    if (
                        _extra_id in _extra_known
                        or _extra_id in active_overrides
                        or _extra_id in expired_pause_entries
                        or _extra_id in held_this_run
                    ):
                        continue
                    _extra_known.add(_extra_id)
                    pause_decisions.append(_extra)
                    logger.info(
                        "run_autopilot_live: %s добавил кандидата %s (%s)",
                        _gen_name,
                        _extra_id,
                        "; ".join(_extra.get("reasons") or []),
                    )
            except Exception as exc:  # noqa: BLE001 — генератор не роняет прогон
                logger.warning(
                    "run_autopilot_live: генератор %s пропущен — %s", _gen_name, exc
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "run_autopilot_live: доп. генераторы кандидатов пропущены — %s", exc
        )

    # Живой квал-страж (регрессия): KB отстаёт от AMO на часы, и реклама
    # с уже пришедшими квалами выглядела «лиды есть, квалов ноль» — в первый
    # день полной автономии так выключались рабочие рекламы с нормальной ценой
    # квала.
    # Перепроверяем живым AMO только кандидатов с лидами и нулевым квалом в KB.
    try:
        from services.live_qual_guard import filter_stale_qual_candidates

        pause_decisions, _stale_qual_dropped = filter_stale_qual_candidates(
            pause_decisions, local_by_id
        )
        if _stale_qual_dropped:
            logger.info(
                "run_autopilot_live: живой квал-страж снял с паузы %d: %s",
                len(_stale_qual_dropped),
                _stale_qual_dropped,
            )
    except Exception as exc:  # noqa: BLE001 — страж не роняет прогон
        logger.warning("run_autopilot_live: живой квал-страж пропущен — %s", exc)

    # Сортируем: confirmed_waster — ПЕРВЫМИ (по убыванию расхода), затем новый
    # hard-stop (старшие первыми), затем остальные по score (худший первым).
    # Это гарантирует что дорогие сливы паузятся до того как лимит съедят вето-темы или слабые рекламы.
    from services import autonomous_pause as autonomous

    # V2-подтверждённые сливы (правила автономии R1/R2) считаем ДО cap-среза и
    # даём им тот же высший класс, что и тир-A: иначе они сидят в группе «по
    # score», срез забивают тир-A и нулевые-лиды, и пересечение автономного
    # пула со срезом получается пустым МОЛЧА — автономия «включена», а
    # исполнений ноль (был такой баг).
    autonomous_v2_pool: set[str] = set()
    try:
        if autonomous.is_autonomous_pause_enabled(cfg):
            autonomous_v2_pool = autonomous.select_autonomous_candidates(
                list(pause_decisions),
                local_by_id,
                use_erp_payments=(
                    ((cfg.get("cdp") or {}).get("payments_source", "shadow")) == "erp"
                ),
                cfg_v2=cfg.get("scaler_v2") or {},
            )
    except Exception as exc:  # noqa: BLE001 — сбой v2-отбора не роняет прогон
        logger.warning(
            "run_autopilot_live: v2-отбор до среза пропущен — %s", exc
        )

    def _pause_priority(decision: dict) -> tuple:
        ad_id = decision["ad_id"]
        local_ad = local_by_id.get(ad_id, {})
        if decision.get("is_confirmed_waster") or str(ad_id) in autonomous_v2_pool:
            return (0, -float(local_ad.get("spend", 0)), ad_id)
        if decision.get("is_zero_leads_after_3d"):
            age_days = _known_age_days(local_ad) or 0
            return (1, -age_days, ad_id)
        return (2, decision["score"], "")

    pause_decisions.sort(key=_pause_priority)

    # Предохранитель автономии смотрит на ПОЛНЫЙ список кандидатов прогона, до
    # среза по cap: срез (8 за прогон) физически не даёт превысить порог в 10
    # штук, и аномалия «сверка сломалась, все разом стали сливами» была бы
    # незаметна. Сам список кандидатов на исполнение по-прежнему режется cap-ом.
    autonomous_candidate_pool = list(pause_decisions)

    # Срезаем до cap
    pause_decisions = pause_decisions[:cap]

    # Ранний выход по «нечего паузить» — теперь учитывает и истёкшие удержания:
    # если ранговых кандидатов нет, но есть истёкшие hold-записи на паузу — идём
    # дальше (Шаг 4 проверит их в FB), иначе истечение никогда не применится.
    if not pause_decisions and not expired_pause_entries:
        logger.info("run_autopilot_live: аутсайдеров нет — паузить нечего")
        send_telegram(
            f"✅ <b>Автопилот Live</b> ({html.escape(trigger)}): "
            f"проанализировано {len(local_ads)} объявлений — аутсайдеров нет"
        )
        return {"ran": True, "skipped": None, "analyzed": len(local_ads),
                "candidates": [], "paused": [], "errors": []}

    # Шаг 4: свежая проверка из FB только по кандидатам (+ истёкшие удержания —
    # им тоже нужно свежее подтверждение ACTIVE перед паузой).
    candidate_ids = [d["ad_id"] for d in pause_decisions] + [
        ad_id for ad_id in expired_pause_entries if ad_id not in {d["ad_id"] for d in pause_decisions}
    ]
    try:
        fb_info = _fetch_candidate_fb_info(candidate_ids)
    except Exception as exc:
        logger.error("run_autopilot_live: _fetch_candidate_fb_info упал: %s", exc)
        send_critical_alert("Автопилот Live: ошибка свежей FB-проверки", str(exc))
        return {
            "ran": False,
            "skipped": f"fb_check_error: {exc}",
            "analyzed": len(local_ads),
            "candidates": [d["ad_id"] for d in pause_decisions],
            "paused": [],
            "errors": [f"fb_check_error: {exc}"],
        }

    # Если FB вернул пустой ответ — не паузить (данные непроверены)
    if not fb_info:
        logger.error("run_autopilot_live: FB вернул пустой ответ для кандидатов — паузы отменены")
        send_critical_alert(
            "Автопилот Live: пустой ответ FB для кандидатов",
            "Паузы отменены — не удалось проверить актуальные статусы кандидатов.",
        )
        return {
            "ran": False,
            "skipped": "fb_empty_response",
            "analyzed": len(local_ads),
            "candidates": [d["ad_id"] for d in pause_decisions],
            "paused": [],
            "errors": ["FB вернул пустой ответ для кандидатов"],
        }

    # Фикс 3: предупреждение если FB вернул не всех кандидатов
    missing_from_fb = [ad_id for ad_id in candidate_ids if ad_id not in fb_info]
    if missing_from_fb:
        logger.warning(
            "run_autopilot_live: FB вернул %d из %d кандидатов; не получены: %s — паузим только проверенных",
            len(fb_info), len(candidate_ids), missing_from_fb,
        )
        _send_critical_alert_non_throwing(
            send_critical_alert,
            "Автопилот Live: неполный ответ FB по кандидатам",
            "PAUSE отсутствующих кандидатов заблокирована: " + ", ".join(missing_from_fb),
        )

    # Истёкшие удержания (Шаг 3.5) добавляем в общий поток как псевдо-decisions —
    # дальше still_active/guardrail/final_list обрабатывают их наравне с обычными
    # ранговыми кандидатами (единый код, без дублирования и без риска повторной
    # обработки). is_expired_hold=True — маркер для Шага 5.5: такой кандидат уже
    # решён (пауза с причиной истечения), should_hold к нему не применяется.
    for ad_id, entry in expired_pause_entries.items():
        # Истёкший hold уже отстоял защитный срок, поэтому имеет приоритет перед
        # обычными кандидатами в групповом N-1 плане. Иначе planner оставит
        # истёкшую рекламу ACTIVE, а запаузит технический filler.
        pause_decisions.insert(0, {
            "ad_id": ad_id,
            "ad_name": entry.get("ad_name", ad_id),
            "reasons": [entry.get("_expire_reason", "удержание истекло")],
            "score": 0,
            "is_confirmed_waster": False,
            "is_early_waster": False,
            "is_expired_hold": True,
        })

    # Фильтруем: оставляем только тех, кто реально ACTIVE в FB
    still_active = [
        d for d in pause_decisions
        if fb_info.get(d["ad_id"], {}).get("effective_status") == "ACTIVE"
    ]

    skipped_not_active = [
        d["ad_id"] for d in pause_decisions
        if d["ad_id"] not in [x["ad_id"] for x in still_active]
    ]
    if skipped_not_active:
        logger.info(
            "run_autopilot_live: %d кандидатов пропущены (не ACTIVE в FB или нет в ответе): %s",
            len(skipped_not_active), skipped_not_active,
        )

    # Шаг 5: полный live inventory всех затронутых adset.
    # Ошибка, неполная пагинация или неизвестный adset блокируют PAUSE.
    try:
        from services.adset_pause_guard import fetch_pause_inventory, plan_safe_pauses
        pause_inventories = fetch_pause_inventory([d["ad_id"] for d in still_active])
        allowed_by_guard, blocked_by_guard = plan_safe_pauses(
            still_active, pause_inventories, id_key="ad_id"
        )
        replacement_ids = {
            str(decision.get("ad_id") or "")
            for decision in blocked_by_guard
            if decision.get("pause_guard_reason") == "last_active_without_replacement"
            and any(
                str(decision.get("ad_id") or "") in (inventory.get("candidate_context") or {})
                and set(inventory.get("active_ids") or set()) == {
                    str(decision.get("ad_id") or "")
                }
                for inventory in pause_inventories.values()
            )
        }
        runnable_ids = {
            str(decision.get("ad_id") or "") for decision in allowed_by_guard
        } | replacement_ids
        final_list = [
            decision
            for decision in still_active
            if str(decision.get("ad_id") or "") in runnable_ids
        ]
        blocked_by_guard = [
            decision
            for decision in blocked_by_guard
            if str(decision.get("ad_id") or "") not in replacement_ids
        ]
    except Exception as exc:
        detail = str(exc).strip()
        guard_reason = f"pause_guard_error:{type(exc).__name__}" + (
            f": {detail}" if detail else ""
        )
        logger.warning("run_autopilot_live: ошибка live inventory: %s", guard_reason)
        _send_critical_alert_non_throwing(
            send_critical_alert,
            "Автопилот Live: ошибка safety guard",
            f"Паузы отмены: {guard_reason}",
        )
        return {
            "ran": False,
            "skipped": guard_reason,
            "analyzed": len(local_ads),
            "candidates": [d["ad_id"] for d in still_active],
            "paused": [],
            "errors": [guard_reason],
        }

    pause_guard_errors = [
        f"pause_guard {d.get('ad_id')}: {d.get('pause_guard_reason')}"
        for d in blocked_by_guard
    ]
    if blocked_by_guard:
        blocked_text = "\n".join(
            f"• {d.get('ad_id')}: {d.get('pause_guard_reason')}"
            for d in blocked_by_guard
        )
        _send_critical_alert_non_throwing(
            send_critical_alert,
            "Автопилот Live: PAUSE заблокированы safety guard",
            blocked_text,
        )

    if not final_list:
        logger.info("run_autopilot_live: все кандидаты защищены guardrail — паузить нечего")
        send_telegram(
            "✅ <b>Автопилот Live</b>: все кандидаты защищены guardrail (adset не должен пустеть)"
        )
        return {"ran": True, "skipped": None, "analyzed": len(local_ads),
                "candidates": [d["ad_id"] for d in still_active],
                "paused": [], "errors": pause_guard_errors}

    # Шаг 5.5: удержание (ARCH-rank-pause-hold) — не паузим сразу «почти-прибыльных»
    # портфельных аутсайдеров, а держим hold_days и смотрим на рост оплат.
    # Истёкшие удержания (is_expired_hold=True) уже прошли через Шаг 3.5 и общий
    # FB/guardrail-поток — здесь их только доводим до конца (чистим hold-стейт от
    # подтверждённых пауз), should_hold к ним НЕ применяется (иначе двойная логика
    # для одного и того же кандидата — баг, найденный тестом T5).
    # Fail-safe: любая ошибка hold-блока → to_pause=final_list (старое поведение),
    # to_hold=[] — деньги важнее, сливы всё равно паузятся ниже как раньше.
    to_pause: list[dict] = final_list
    to_hold: list[dict] = []
    held: list[dict] = []
    if cfg.get("hold_enabled"):
        # Обогащаем local_ads встречами ДО should_hold (ARCH-hold-meetings) — только
        # когда удержание включено и есть ранговые кандидаты (final_list не пуст),
        # иначе лишний AMO-запрос не нужен. local_by_id ссылается на те же dict-объекты
        # local_ads, поэтому should_hold ниже увидит meetings_scheduled/meetings_held
        # через ad_metrics = local_by_id.get(ad_id, {}).
        _enrich_meetings_for_hold(local_ads)
        try:
            from services.autopilot_hold import (
                load_hold_state, save_hold_state, is_held,
                should_hold, make_hold_entry,
            )

            hold_state = load_hold_state()
            holds = hold_state.get("holds", {})
            held_this_run |= {str(ad_id) for ad_id in holds}
            state_changed = False

            new_to_pause: list[dict] = []
            for decision in final_list:
                ad_id = decision["ad_id"]

                if decision.get("is_zero_leads_after_3d"):
                    if ad_id in holds:
                        del holds[ad_id]
                        state_changed = True
                    new_to_pause.append(decision)
                    continue

                if decision.get("is_expired_hold"):
                    # Подтверждённая ACTIVE-пауза истёкшего удержания — просто
                    # чистим служебную запись из hold-стейта (если ещё не удалена)
                    # и отправляем в паузу как есть.
                    if ad_id in holds:
                        del holds[ad_id]
                        state_changed = True
                    new_to_pause.append(decision)
                    continue

                # Кандидат уже в активном удержании (is_held) — пропускаем совсем:
                # не паузим, не дублируем hold-запись, ждём истечения следующим прогоном.
                if is_held(ad_id, hold_state):
                    continue

                ad_metrics = local_by_id.get(ad_id, {})
                do_hold, hold_reason = should_hold(decision, ad_metrics, cfg)
                if do_hold:
                    entry = make_hold_entry(decision, ad_metrics, cfg)
                    holds[ad_id] = entry
                    state_changed = True
                    to_hold.append(decision)
                    held.append({
                        "ad_id": ad_id,
                        "ad_name": entry["ad_name"],
                        "hold_until": entry["hold_until"],
                        "reason": entry["reason"],
                        "romi": entry["romi"],
                        "spend": entry["spend"],
                        "qual_pct": entry["qual_pct"],
                        # Встречи (ARCH-hold-meetings) — для телеметрии Telegram (_format_held_section)
                        "meetings_scheduled": entry["meetings_scheduled"],
                        "meetings_held": entry["meetings_held"],
                    })
                    logger.info(
                        "run_autopilot_live: удержание поставлено %s (%s) до %s",
                        ad_id, entry["ad_name"], entry["hold_until"],
                    )
                else:
                    new_to_pause.append(decision)

            to_pause = new_to_pause

            if state_changed:
                hold_state["holds"] = holds
                save_hold_state(hold_state)
        except Exception as exc:
            logger.error("run_autopilot_live: ошибка hold-блока (fail-safe — паузим как раньше): %s", exc)
            send_critical_alert(
                "Автопилот Live: ошибка удержания",
                f"Удержание пропущено в этом прогоне, кандидаты паузятся как раньше: {exc}",
            )
            to_pause = final_list
            to_hold = []
            held = []
            # Кандидаты под активным удержанием попали обратно в to_pause. Это
            # приемлемо для предложений владельцу, но НЕ для автономии: иначе
            # сбой hold-блока молча выключал бы рекламу, которую удержание
            # защищает (реклама могла стать confirmed_waster уже ПОСЛЕ
            # постановки в удержание — тогда она проходит и автономный отбор).
            hold_state_trusted = False

    if not to_pause:
        logger.info("run_autopilot_live: после удержания паузить нечего")
        _send_live_telegram_report(
            paused_details=[],
            analyzed=len(local_ads),
            trigger=trigger,
            errors=pause_guard_errors,
            held=held,
            trend_note=_format_trend_note(decisions),
        )
        return {"ran": True, "skipped": None, "analyzed": len(local_ads),
                "candidates": [d["ad_id"] for d in still_active],
                "paused": [], "autonomous": [],
                "errors": pause_guard_errors, "held": held,
                **_trend_telemetry(decisions, trend_ctx)}

    # Шаг 5.7: какие из оставшихся кандидатов бот имеет право выключить САМ.
    # Автономия узкая (решение владельца): только подтверждённый слив и
    # только если мастер-ключ включён. Всё остальное — по-прежнему предложение
    # с кнопкой. Аномальный всплеск кандидатов (сбой данных) обнуляет список
    # целиком и будит владельца — см. services/autonomous_pause.py.
    autonomous_ids: set[str] = set()
    try:
        if autonomous.is_autonomous_pause_enabled(cfg):
            # Пул v2 уже посчитан до cap-среза (см. _pause_priority) — второй
            # сетевой прогон правил дал бы только шанс на рассинхрон.
            pool = autonomous_v2_pool
            if autonomous.is_full_pause_autonomy_enabled(cfg):
                # Полная автономия (решение владельца): исполняем все
                # pause-кандидаты, а не только подтверждённые сливы v2.
                pool = {str(d["ad_id"]) for d in to_pause}
            autonomous_ids = pool & {str(d["ad_id"]) for d in to_pause}
            # Удержание автономией не обходится. Нет достоверного hold-стейта —
            # нет автономных действий вообще (fail-closed); есть — исключаем всё,
            # что этот прогон застал под удержанием.
            if not hold_state_trusted:
                if autonomous_ids:
                    logger.warning(
                        "run_autopilot_live: автономные паузы отменены — hold-стейт "
                        "недостоверен, кандидаты уедут владельцу предложением: %s",
                        sorted(autonomous_ids),
                    )
                autonomous_ids = set()
            elif held_this_run & autonomous_ids:
                blocked_by_hold = sorted(held_this_run & autonomous_ids)
                autonomous_ids -= held_this_run
                logger.info(
                    "run_autopilot_live: под удержанием — автономно не выключаем, "
                    "предложение владельцу: %s",
                    blocked_by_hold,
                )
            is_anomaly, anomaly_detail = autonomous.check_anomaly(
                len(pool),
                enabled=autonomous.is_anomaly_guard_enabled(cfg),
            )
            if is_anomaly:
                autonomous_ids = set()
                autonomous.send_anomaly_alert(anomaly_detail)
                logger.error(
                    "run_autopilot_live: автономные паузы остановлены — %s",
                    anomaly_detail,
                )
    except Exception as exc:  # noqa: BLE001 — сбой автономии не роняет предложения
        autonomous_ids = set()
        logger.warning("run_autopilot_live: автономный отбор пропущен — %s", exc)

    # Шаг 6: только owner proposals, без Facebook mutation
    from services.action_producer_gateway import propose_pause
    from services.approval_checker_models import ActionOrigin
    from services.owner_proposal_card import DecisionContext

    paused_ids: list[str] = []
    paused_details: list[dict] = []
    autonomous_details: list[dict] = []
    proposal_ids: list[str] = []
    # Сколько кандидатов уже имеют висящее предложение: они не идут в отчёт,
    # чтобы один и тот же список не приходил владельцу каждые 15 минут.
    already_pending = 0
    errors: list[str] = list(pause_guard_errors)

    action_run_id = str(uuid4())
    for decision in to_pause:
        ad_id = decision["ad_id"]
        ad_name = decision.get("ad_name", ad_id)
        reason = "; ".join(decision.get("reasons", []))
        local_ad = local_by_id.get(ad_id, {})

        try:
            pause_outcome = propose_pause(
                ad_id,
                origin=ActionOrigin.AUTOPILOT_LIVE,
                scope=f"autopilot-live:{action_run_id}:{ad_id}",
                reason_code="AUTOPILOT_LIVE",
                # Те же цифры, что и в Live-отчёте (_format_live_pause_block):
                # владелец должен видеть их в самой карточке, а не только в
                # сводке — раньше карточка приходила без единого числа.
                decision=DecisionContext(
                    spend_usd=local_ad.get("spend"),
                    leads=local_ad.get("leads"),
                    cpl_usd=local_ad.get("cpl"),
                    qual_pct=local_ad.get("qual_pct"),
                    payments=local_ad.get("payments"),
                    business_reason=decision.get("business_reason") or reason,
                ),
            )
            if pause_outcome.receipt is not None:
                proposal_ids.append(pause_outcome.receipt.proposal_id)
                if pause_outcome.receipt.deduplicated and ad_id not in autonomous_ids:
                    # Предложение по этой рекламе уже висит у владельца и ждёт
                    # решения. В отчёт её не кладём: иначе каждые 15 минут
                    # приходит один и тот же список (жалоба владельца на
                    # повторы). Тот же приём уже применяет
                    # budget_scaler (:2503).
                    # ВАЖНО: автономных кандидатов этот выход не касается — у
                    # v2-слива карточка почти всегда уже висит с прошлых дней,
                    # и ранний continue означал «самоодобрение никогда» (был
                    # такой баг): бот слал предложения вместо исполнения.
                    already_pending += 1
                    logger.info(
                        "run_autopilot_live: proposal по %s уже существует — в отчёт не дублируем",
                        ad_id,
                    )
                    continue
                metrics = {
                    "id": ad_id,
                    "name": ad_name,
                    "reason": f"Предложение владельцу: {reason}",
                    "business_reason": decision.get("business_reason") or "",
                    "score": decision["score"],
                    "spend": local_ad.get("spend", 0),
                    "leads": local_ad.get("leads"),
                    "cpl": local_ad.get("cpl"),
                    "qual_pct": local_ad.get("qual_pct"),
                    "payments": local_ad.get("payments"),
                }
                # Автономный класс: бот сам принимает решение и ставит его в ту
                # же очередь исполнения. Отказ самоодобрения безопасен —
                # предложение остаётся обычным и уедет владельцу с кнопками.
                self_approved = False
                if ad_id in autonomous_ids:
                    self_approved = autonomous.approve_autonomously(
                        pause_outcome.receipt.proposal_id,
                        evidence={
                            "ad_id": ad_id,
                            "ad_name": ad_name,
                            "spend_usd": local_ad.get("spend"),
                            "leads": local_ad.get("leads"),
                            "qual_pct": local_ad.get("qual_pct"),
                            "payments": local_ad.get("payments"),
                            "outcomes_matched_at": local_ad.get("outcomes_matched_at"),
                            "business_reason": decision.get("business_reason") or reason,
                            "rule_reasons": decision.get("reasons", []),
                        },
                    )
                if self_approved:
                    entry = {
                        **metrics,
                        "ad_id": ad_id,
                        "reason": f"Выключено автоматически: {reason}",
                        "proposal_id": pause_outcome.receipt.proposal_id,
                    }
                    autonomous_details.append(entry)
                    autonomous.record_autonomous_pause(entry)
                    logger.info(
                        "run_autopilot_live: %s выключен автономно (proposal %s)",
                        ad_id,
                        pause_outcome.receipt.proposal_id,
                    )
                elif pause_outcome.receipt.deduplicated:
                    # Автономный кандидат с уже висящей карточкой, самоодобрить
                    # которую не вышло (например, terminal-состояние): карточка
                    # у владельца уже есть, второй раз в отчёт не кладём.
                    already_pending += 1
                    logger.info(
                        "run_autopilot_live: %s — самоодобрение существующего "
                        "proposal %s не прошло, остаётся кнопкой у владельца",
                        ad_id,
                        pause_outcome.receipt.proposal_id,
                    )
                else:
                    # В отчёт «жду решения» попадают только настоящие
                    # предложения: автономные там были бы враньём.
                    paused_details.append(metrics)
                    logger.info(
                        "run_autopilot_live: proposal %s создан для %s",
                        pause_outcome.receipt.proposal_id,
                        ad_id,
                    )
            else:
                msg = f"PAUSE proposal {ad_id} не создан: {pause_outcome.reason} ({ad_name})"
                logger.warning(msg)
                errors.append(msg)
                _send_critical_alert_non_throwing(
                    send_critical_alert,
                    "Автопилот Live: PAUSE заблокирована safety guard",
                    msg,
                )
                break
        except Exception as exc:
            detail = str(exc).strip()
            msg = f"pause {ad_id}: pause_guard_error:{type(exc).__name__}" + (
                f": {detail}" if detail else ""
            )
            logger.error(msg)
            errors.append(msg)
            _send_critical_alert_non_throwing(
                send_critical_alert,
                "Автопилот Live: ошибка создания PAUSE proposal",
                msg,
            )
            break

    # Шаг 7: дневной счётчик пауз здесь больше не растёт. Прогон только создаёт
    # предложения (paused_ids всегда пуст), а _load_live_daily_state берёт кап
    # дня из AUTO_ACTION-проекций подтверждённых эффектов — то есть считает
    # исполненные после одобрения паузы, а не отправленные предложения.

    # Шаг 8: Telegram-отчёт с кнопками «Вернуть» + секция удержания
    _send_live_telegram_report(
        paused_details=paused_details,
        analyzed=len(local_ads),
        trigger=trigger,
        errors=errors,
        held=held,
        already_pending=already_pending,
        trend_note=_format_trend_note(decisions),
    )

    return {
        "ran": True,
        "skipped": None,
        "analyzed": len(local_ads),
        "candidates": [d["ad_id"] for d in still_active],
        "paused": paused_ids,
        "proposals": proposal_ids,
        # Что бот выключил сам (подтверждённые сливы). Сводка по ним уходит
        # владельцу одним вечерним сообщением, а не после каждого прогона.
        "autonomous": [item["ad_id"] for item in autonomous_details],
        "errors": errors,
        "held": held,
        **_trend_telemetry(decisions, trend_ctx),
    }


def _format_held_section(held: list[dict]) -> str:
    """Формирует секцию Telegram-отчёта «⏸→🕐 Держу N (встречи/оплаты)».

    Формат блока (ARCH-rank-pause-hold §7 + ARCH-hold-meetings §7, деньги — через fmt_money):
    • {ad_name} — до {DD.MM}, {M} встреч (назн. {A} / сост. {B}), ROMI {romi:.0f}%, расход {fmt_money(spend)}, квал {qual_pct:.0f}%

    Блок «{M} встреч (назн. A / сост. B)» печатается ТОЛЬКО если M = scheduled+held > 0
    (иначе — старый текст фазы 1, без упоминания встреч).
    """
    lines = []
    for d in held:
        ad_name = d.get("ad_name", "") or ""
        if len(ad_name) > 40:
            ad_name = ad_name[:40]
        hold_until_dt = _parse_iso(d.get("hold_until"))
        date_str = hold_until_dt.strftime("%d.%m")

        romi = d.get("romi")
        spend = d.get("spend")
        qual_pct = d.get("qual_pct")
        romi_str = f"{romi:.0f}%" if romi is not None else "—"
        spend_str = fmt_money(spend, "$") if spend is not None else "—"
        qual_str = f"{qual_pct:.0f}%" if qual_pct is not None else "—"
        reason_short = f"ROMI {romi_str}, расход {spend_str}, квал {qual_str}"

        # Встречи (ARCH-hold-meetings) — печатаем блок только если есть хоть одна
        meetings_scheduled = int(d.get("meetings_scheduled", 0) or 0)
        meetings_held = int(d.get("meetings_held", 0) or 0)
        total_meetings = meetings_scheduled + meetings_held
        meetings_part = (
            f"{total_meetings} встреч (назн. {meetings_scheduled} / сост. {meetings_held}), "
            if total_meetings > 0 else ""
        )

        lines.append(f"• {html.escape(ad_name)} — до {date_str}, {meetings_part}{reason_short}")

    body = "\n".join(lines)
    return f"⏸→🕐 <b>Держу {len(held)} (встречи/оплаты)</b>\n{body}"


# Словарь перевода скоринговых причин decision_policy.score_and_decide в
# человеческие фразы (владелец жаловался — «hook/ctr не в (¤ 67)» нечитаемо).
# Ключ — характерная подстрока причины (как её пишет score_and_decide),
# значение — человеческая формулировка. Проверяем by-substring (не точным
# совпадением), т.к. многие причины содержат числа/интерполяцию.
_REASON_HUMAN_MAP: list[tuple[str, str]] = [
    (
        "PAUSE: 3+ полных дня с запуска и 0 lifetime-лидов",
        "за 3+ полных дня с запуска не получено ни одного лида",
    ),
    (
        "видео: нет паттерна «широкий вход»",
        "видео не цепляет с первых секунд (нет широкого входа) или мало данных",
    ),
    (
        "hook/ctr не выше медианы группы",
        "хуже похожих реклам по цепляемости (hook/CTR)",
    ),
    (
        "нет ключевых слов эмоции/оффера в названии",
        "в названии нет зацепки на эмоцию/оффер",
    ),
    (
        "PAUSE: есть лиды, но qual_pct=0",
        "лиды есть, но ни один не квалифицировался",
    ),
    (
        "PAUSE: подтверждённый слив",
        "подтверждённый слив бюджета — лиды есть, оплат нет",
    ),
    (
        "PAUSE: портфельный аутсайдер",
        "слабее аналогов в своей группе (город/тип)",
    ),
    (
        "PAUSE: ранний слив",
        "слив в первые дни — расход растёт, лидов нет",
    ),
    (
        "расход+лиды, но 0 матчей AMO",
        "лиды не долетают до CRM (сломана связка) 3+ дня",
    ),
    (
        "KEEP: недостаточно данных",
        "не хватало данных для уверенного решения",
    ),
]


def _humanize_pause_reason(reason: str) -> str:
    """Переводит сырую причину score_and_decide в человеческую фразу.

    Причина обычно склеена из нескольких сработавших правил через "; ".
    Каждую часть прогоняем через _REASON_HUMAN_MAP (замена по подстроке);
    части без перевода оставляем как есть (лучше сырой текст, чем пропуск
    причины). Технические метки "+2:", "PAUSE:" в начале строки убираем.
    """
    if not reason:
        return "без причины"

    parts = [p.strip() for p in reason.split(";") if p.strip()]
    human_parts = []
    for part in parts:
        translated = part
        for pattern, human in _REASON_HUMAN_MAP:
            if pattern in part:
                translated = human
                break
        else:
            # Перевода нет — убираем служебные префиксы "+2:", "PAUSE:", "dry_run:"
            translated = part
            for prefix in ("PAUSE: ", "KEEP: ", "dry_run: "):
                if translated.startswith(prefix):
                    translated = translated[len(prefix):]
                    break
        human_parts.append(translated)

    return "; ".join(human_parts)


def _format_live_pause_block(index: int, d: dict) -> str:
    """Один блок Live-отчёта об автопаузе (эталон _format_pause_report).

    Формат:
        {index}. {имя, обрезка по слову}
           💸 {расход} · {N} {лид/лида/лидов} · CPL {cpl}
           👥 квал {K} ({Y}%) · оплат {Z}
           📉 {бизнес-причина}

    None-поля печатаем как «нет данных» (НЕ 0, НЕ «—») — решение владельца.
    Деньги — через fmt_money. Причина — business_reason из
    decision_policy (бизнес-часть ПЕРВОЙ), с fallback на _humanize_pause_reason.
    """
    name = truncate_at_word_boundary(d.get("name", "") or "", 60)

    spend_str = fmt_money(d.get("spend"), "$")

    leads = d.get("leads")
    leads_str = f"{leads} {pluralize_leads(leads)}" if leads is not None else "лиды: нет данных"

    cpl = d.get("cpl")
    # CPL=0 при расходе — дыра данных (для 0 лидов CPL не определён), пишем «нет данных»
    cpl_str = f"CPL {fmt_money(cpl, '$')}" if cpl else "CPL нет данных"

    qual_pct = d.get("qual_pct")
    leads_n = leads or 0
    if qual_pct is not None and leads_n:
        qual_n = round(qual_pct / 100 * leads_n)
        qual_str = f"квал {qual_n} ({qual_pct:.0f}%)"
    elif qual_pct is not None:
        qual_str = f"квал {qual_pct:.0f}%"
    else:
        qual_str = "квал нет данных"

    payments = d.get("payments")
    payments_str = f"оплат {payments}" if payments is not None else "оплат нет данных"

    business = d.get("business_reason") or _humanize_pause_reason(d.get("reason", ""))
    business = truncate_at_word_boundary(business, 200)

    return (
        f"{index}. {html.escape(name)}\n"
        f"   💸 {spend_str} · {leads_str} · {cpl_str}\n"
        f"   👥 {qual_str} · {payments_str}\n"
        f"   📉 {html.escape(business)}"
    )


def _count_trend_effect(decisions: list[dict]) -> tuple[int, int]:
    """(усилено паузой, усилило бы в тени) по решениям одного прогона."""
    reinforced = sum(1 for d in decisions if d.get("trend_reinforced"))
    shadow = sum(1 for d in decisions if d.get("trend_shadow"))
    return reinforced, shadow


def _trend_telemetry(decisions: list[dict], trend_ctx) -> dict:
    """Счётчики тренда в результате прогона — та же тройка, что в отчёте."""
    reinforced, shadow = _count_trend_effect(decisions)
    mode = "none"
    if trend_ctx is not None:
        try:
            mode = trend_ctx.mode.value
        except Exception:  # pragma: no cover — чужой объект в тестах
            mode = "none"
    return {
        "trend_mode": mode,
        "trend_reinforced": reinforced,
        "trend_shadow_reinforced": shadow,
    }


def _log_trend_effect(decisions: list[dict], trend_ctx) -> None:
    """Пишет в лог, что тренд сделал за прогон. Никогда не роняет прогон."""
    try:
        reinforced, shadow = _count_trend_effect(decisions)
        counters = trend_ctx.counters() if trend_ctx is not None else {}
        logger.info(
            "run_autopilot_live: тренд — режим %s, адсетов %s, падений с правом "
            "действовать %s, пауз усилено %d, усилило бы в тени %d",
            counters.get("mode"), counters.get("adsets"),
            counters.get("action_grade_declines"), reinforced, shadow,
        )
    except Exception as exc:  # pragma: no cover — наблюдаемость не критична
        logger.warning("run_autopilot_live: не удалось залогировать тренд — %s", exc)


def _format_trend_note(decisions: list[dict]) -> str:
    """Хвост отчёта про тренд (волна 3). "" — тренду сказать нечего.

    Показывает ровно то, что тренд сделал: сколько пауз усилено и сколько
    усилил бы, будь режим active. Теневые цифры помечены явно, чтобы «тень»
    не читалась как исполненное действие.
    """
    reinforced, shadow = _count_trend_effect(decisions)
    parts = []
    if reinforced:
        parts.append(f"\n\n📉 Тренд усилил пауз: {reinforced}")
    if shadow:
        parts.append(f"\n\n📉 Тренд усилил бы пауз: {shadow} (тень: не применено)")
    return "".join(parts)


def _send_live_telegram_report(
    paused_details: list[dict],
    analyzed: int,
    trigger: str,
    errors: list[str],
    held: list[dict] | None = None,
    already_pending: int = 0,
    trend_note: str = "",
) -> None:
    """Формирует и отправляет Telegram-отчёт об автопаузах с кнопками «Вернуть».

    При наличии паузы — отправляет send_with_buttons (кнопка на каждую, максимум
    MAX_PAUSE_UNDO_BUTTONS — как в ранговом отчёте, _build_pause_undo_buttons).
    При пустом списке пауз (и пустых held/errors) — короткое «аутсайдеров нет».
    Если held не пуст — добавляет секцию «⏸→🕐 Держу N», даже если пауз нет.
    Если errors не пуст — секция «⚠️ Ошибки» уходит владельцу даже без пауз:
    «аутсайдеров нет» при заблокированной паузе маскировало бы проблему.

    Формат блока — _format_live_pause_block (редизайн, эталон
    _format_pause_report): бизнес-причина впереди, квал/оплаты видны, score
    и творческий жаргон (hook/CTR) в Telegram больше не выводятся.

    Кнопка «Вернуть» — callback_data "undo_pause:<ad_id>" (обрабатывается
    services.telegram_bot._execute_undo_pause: создаёт UNPAUSE-предложение
    владельцу, сам FB не трогает). Отличается от старого "undo:<ad_id>"
    (используется отдельно evening_report'ом — не трогаем).
    """
    from services.notifications import send_telegram

    held = held or []
    held_section = f"\n\n{_format_held_section(held)}" if held else ""
    error_note = ""
    if errors:
        error_note = f"\n⚠️ Ошибки ({len(errors)}): {html.escape(str(errors[0]))}"

    # Решение владельца: днём — тишина, всё утром одним заходом.
    # Предложения серой зоны и так копятся в дайджест 9:00 (owner_delivery,
    # approval.digest_hour), автономные исполнения — в сводку 9:00. Слотовые
    # отчёты «предлагаю паузу — жду решения» каждые 2 часа он назвал спамом.
    # Единственное, что не ждёт утра, — ошибки: заблокированная пауза,
    # замолчанная до 9:00, стоила бы денег.
    if errors:
        send_telegram(f"🤖 <b>Автопилот Live</b>{error_note}{held_section}")
        return
    if paused_details or held or already_pending:
        logger.info(
            "run_autopilot_live: отчёт слота не шлём (решение владельца) — "
            "%d предложений уйдут дайджестом 9:00, %d под удержанием",
            len(paused_details) + already_pending,
            len(held),
        )
    return

