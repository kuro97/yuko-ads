"""
Budget Scaler — поднимает дневной бюджет адсетов с ДОКАЗАННЫМИ продажами.

ОПАСНЫЙ МОДУЛЬ: напрямую увеличивает расходы.
Поэтому все предохранители обязательны и нельзя их отключать:

1. scale_enabled (дефолт FALSE) — мастер-ключ масштабирования.
   Без него только dry_run рекомендации, НИКОГДА реальных изменений.
2. enabled + НЕ kill_switch — ещё два уровня блокировки.
3. max_budget_increase_pct — максимальный процент роста бюджета ЗА КАЛЕНДАРНЫЕ
   ЛОКАЛЬНЫЕ СУТКИ НА КАЖДЫЙ АДСЕТ (дефолт 15%). Несколько подъёмов в день
   допустимы, пока их сумма ≤ лимита — учёт ведёт services.budget_daily_cap.
4. max_adset_budget_mult — потолок-множитель от бюджета НАЧАЛА ДНЯ (дефолт 2.0×).
5. max_adset_daily_budget — абсолютный потолок бюджета одного адсета ($).
6. max_total_daily_budget — потолок суммарного дневного бюджета ВСЕХ адсетов аккаунта ($).
7. max_scales_per_run — максимум адсетов за прогон (дефолт 2).
8. Только ACTIVE — любой другой статус (PAUSED, CAMPAIGN_PAUSED и т.д.) не трогаем.
9. Бюджеты только ВВЕРХ — никакого понижения.
10. Кулдаун 4 часа между реальными повышениями (только active-режим,
    дополнительная защита ПОВЕРХ дневного капа).
11. Кандидат на подъём — адсет, где у объявлений есть РЕАЛЬНЫЕ ОПЛАТЫ
    (payments > 0 за окно сверки AMO ≈2 мес, см. amo_outcomes.OUTCOMES_MONTHS_BACK),
    а не просто хороший CPL/qual/romi. Адсет в learning-фазе (days_running<3)
    исключается целиком. СТРОГОЕ вето слива (all-or-nothing, инвариант Фазы 2):
    ОДИН значимый слив внутри адсета исключает ВЕСЬ адсет из подъёма — никакого
    «долива в слив», даже если рядом сильные победители. «Значимый слив» =
    confirmed_waster (сверено И effective payments==0) И расход ≥ waster_min_spend_usd
    (autopilot.scaler_v2.waster_min_spend_usd, дефолт $10): молодое объявление с
    околонулевым расходом (<$10, оплат 0) сливом НЕ считается и адсет не блокирует.
    Это НЕ доля/процент/большинство — one-strike, уточнён лишь порог значимости
    самого слива (см. _adset_waster_veto).
"""

import html
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # fcntl есть на прод-Linux; на Windows нет — деградируем до threading-lock
    import fcntl
except ImportError:  # pragma: no cover — прод только Linux
    fcntl = None

from services import budget_daily_cap
from services import cdp_client
from services import engine_selfcheck
from services import pacing_curve
from services import trend_gate
from services.cdp_client import CdpError
from services.formatting import fmt_money, truncate_at_word_boundary

logger = logging.getLogger(__name__)

# Локальный часовой пояс (UTC+5 по умолчанию, настраивается здесь)
_TZ_LOCAL = timezone(timedelta(hours=5))

# ---------------------------------------------------------------------------
# Замок ОДНОГО активного прогона (Wave 1B.1)
# ---------------------------------------------------------------------------
# Гарантирует, что в любой момент времени идёт максимум ОДИН active-прогон
# масштабирования — и внутри процесса (threading.Lock), и между процессами
# (flock на стабильном .lock-файле). Замок держится на ВЕСЬ active-прогон,
# включая обращения к FB и финальную запись состояния.
#
# Важное свойство: если future.result(timeout=...) в кроне вернул управление по
# таймауту, СТАРЫЙ поток продолжает держать этот замок до фактического конца.
# Поэтому следующий cron-tick, вызвав run_budget_scaling("active"), не получит
# замок (неблокирующе) и завершится безопасным skipped_reason — второго
# параллельного active-прогона и второй мутации FB не будет.
#
# Путь .lock-файла выводится из budget_daily_cap._CAP_STATE_FILE в момент вызова:
# тесты изолируют cap-state в tmp → и этот замок автоматически ложится в tmp.
_ACTIVE_RUN_TLOCK = threading.Lock()


def _active_run_lock_path() -> Path:
    """Стабильный .lock-файл active-прогона (рядом с cap-state)."""
    return budget_daily_cap._CAP_STATE_FILE.with_name("budget_scaler_active.lock")


class _ActiveRunLock:
    """Держатель замка active-прогона. release() снимает flock и threading-lock."""

    def __init__(self, fd: int | None):
        self._fd = fd
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            if self._fd is not None and fcntl is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    os.close(self._fd)
                except Exception:
                    pass
        finally:
            try:
                _ACTIVE_RUN_TLOCK.release()
            except RuntimeError:
                pass


def _try_acquire_active_run_lock() -> "_ActiveRunLock | None":
    """Неблокирующе берёт замок active-прогона (threading + flock).

    Возвращает держатель или None, если замок уже занят (идёт другой active-прогон)
    либо произошла ошибка захвата (тогда — fail-closed skip, FB не трогаем).
    """
    if not _ACTIVE_RUN_TLOCK.acquire(blocking=False):
        return None  # уже занят в этом процессе
    if fcntl is None:  # pragma: no cover — платформа без fcntl: только внутрипроцессно
        return _ActiveRunLock(None)
    fd = None
    try:
        path = _active_run_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Занят другим процессом — снимаем свои захваты и уходим в skip
            os.close(fd)
            _ACTIVE_RUN_TLOCK.release()
            return None
        return _ActiveRunLock(fd)
    except Exception as exc:
        logger.error("Budget Scaler: ошибка захвата active-lock — fail-closed skip: %s", exc)
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            _ACTIVE_RUN_TLOCK.release()
        except RuntimeError:
            pass
        return None

# State-файл для хранения last_scaled_at (кулдаун)
_SCALE_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "budget_scaler_state.json"

# Кулдаун между реальными повышениями (только active-режим)
_COOLDOWN_HOURS = 4

# ---------------------------------------------------------------------------
# Получение недельного FB-расхода из аналитика-кеша / прямого FB-запроса
# ---------------------------------------------------------------------------

def get_fb_week_spend(date_from: str, date_to: str) -> float:
    """Суммарный расход FB за диапазон дат (из аккаунт-уровня).

    Сначала пробует аналитика-кеш (data/analytics_cache.json), при неудаче —
    прямой FB-запрос account-level insights (аналогично ads_watchdog).

    Args:
        date_from: строка YYYY-MM-DD (начало периода, включительно).
        date_to: строка YYYY-MM-DD (конец периода, включительно).

    Returns:
        Суммарный расход в USD. При ошибке — поднимает исключение.
    """
    # Пробуем взять из analytics-кеша (если покрывает нужный период)
    try:
        _ANALYTICS_CACHE = Path(__file__).resolve().parent.parent / "data" / "analytics_cache.json"
        if _ANALYTICS_CACHE.exists():
            disk = json.loads(_ANALYTICS_CACHE.read_text(encoding="utf-8"))
            cache_from = disk.get("date_from")
            cache_to = disk.get("date_to")
            if cache_from and cache_to and cache_from <= date_from and cache_to >= date_to:
                ads = disk.get("data", [])
                total_spend = sum(float(ad.get("spend", 0) or 0) for ad in ads)
                logger.info(
                    "get_fb_week_spend: из кеша аналитики (%s–%s) → $%.2f",
                    date_from, date_to, total_spend,
                )
                return total_spend
    except Exception as exc:
        logger.warning("get_fb_week_spend: кеш недоступен — %s", exc)

    # Fallback: прямой FB-запрос
    import json as _json
    from agent.fb_common import _throttled_get, API
    from services.fb_token_provider import get_fb_token, get_fb_account_id

    resp = _throttled_get(
        f"{API}/act_{get_fb_account_id()}/insights",
        params={
            "access_token": get_fb_token(),
            "level": "account",
            "fields": "spend",
            "time_range": _json.dumps({"since": date_from, "until": date_to}),
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"FB insights вернул {resp.status_code}: {resp.text[:100]}")
    data = resp.json()
    rows = data.get("data", [])
    if not rows:
        return 0.0
    spend = float(rows[0].get("spend", 0) or 0)
    logger.info("get_fb_week_spend: FB %s–%s → $%.2f", date_from, date_to, spend)
    return spend


# ---------------------------------------------------------------------------
# Получение недельной выручки AMO (¤)
# ---------------------------------------------------------------------------

def get_google_week_spend(date_from, date_to) -> float:
    """Недельный расход Google Ads из сохранённых дневных снимков (services.google_spend).

    Принимает datetime.date или строки YYYY-MM-DD — приводим к ISO перед вызовом.
    """
    from services.google_spend import get_google_week_spend as _impl
    # Приводим к строкам ISO (вызов из budget_scaler передаёт datetime.date)
    from_iso = date_from.isoformat() if hasattr(date_from, "isoformat") else str(date_from)
    to_iso = date_to.isoformat() if hasattr(date_to, "isoformat") else str(date_to)
    return _impl(from_iso, to_iso)


def get_amo_week_revenue(date_from: datetime, date_to: datetime) -> float:
    """Суммарная выручка из AMO CRM за диапазон (field price, статусы оплачено).

    Запрашивает лиды через get_leads_window, фильтрует по classify_lead='оплата'.
    При ошибке AMO — поднимает исключение (не молчим, это критично для гейта).

    Args:
        date_from: начало периода (datetime, локальная TZ).
        date_to: конец периода (datetime, локальная TZ).

    Returns:
        Суммарная выручка в LCY (float).
    """
    from integrations.amo import get_leads_window, classify_lead

    from_ts = int(date_from.timestamp())
    to_ts = int(date_to.timestamp())

    leads = get_leads_window(from_ts, to_ts)
    revenue = 0.0
    for lead in leads:
        # Считаем только оплаченных (classify_lead использует AMO_PAYMENT_STATUS_IDS из config)
        if classify_lead(lead) == "оплата":
            revenue += float(lead.get("price", 0) or 0)

    logger.info(
        "get_amo_week_revenue: %d лидов, выручка ¤%.0f",
        len(leads), revenue,
    )
    return revenue


# ---------------------------------------------------------------------------
# Trailing-окно ДРР: 7 полных дней до последней прошедшей субботы
# ---------------------------------------------------------------------------

def compute_drr_window(now: datetime) -> tuple[datetime, datetime, datetime]:
    """Trailing-окно ДРР: 7 полных дней, заканчивающихся последней прошедшей субботой.

    Логика «денежной субботы»: выручка (оплаты AMO) концентрируется в субботу,
    а расход капает равномерно. Поэтому ДРР текущего частичного окна до субботы
    завышен. Считаем ДРР по стабильному trailing-окну, где в знаменателе всегда
    есть хотя бы одна завершённая суббота.

    Определение «последней прошедшей субботы» (weekday()==5):
      - Если now — суббота: берём ЭТУ субботу как завершённую (окно включает её) —
        прогоны идут в течение дня, но деньги субботы уже начали заходить.
      - Иначе: берём ближайшую субботу СТРОГО в прошлом (1..6 дней назад).

    window_end   = last_saturday в 23:59:59 (локальное время)
    window_start = (last_saturday − 6 дней) в 00:00:00 (локальное время)  # ровно 7 дат вкл.
    last_saturday = дата субботы (для label в Telegram)

    Args:
        now: текущий момент (aware, локальная TZ). Если naive — трактуем как локальное время.

    Returns:
        (window_start, window_end, last_saturday) — все datetime, локальная TZ.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_LOCAL)

    # weekday(): понедельник=0 ... суббота=5, воскресенье=6
    days_since_saturday = (now.weekday() - 5) % 7  # сб=0, вс=1, пн=2, ..., пт=6
    last_saturday = now - timedelta(days=days_since_saturday)

    window_end = last_saturday.replace(hour=23, minute=59, second=59, microsecond=0)
    window_start = (last_saturday - timedelta(days=6)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    return window_start, window_end, last_saturday


# Допуск план-темпа (Шаг A, УСТАРЕЛ): раньше гейт был fact_pace >= time_pct*0.9.
# Заменён сезонным гейтом (Шаг A.2, ARCH-cdp-seasonal-pacing §6.2) — константа
# больше НЕ используется в гейте, оставлена на случай внешних ссылок.
_PLAN_PACE_TOLERANCE = 0.9

# Буфер сезонного план-гейта (Шаг A.2). Кривая pacing_curve.PACING_CURVE — уже
# минимум из 12 наблюдений (2 среза × 6 месяцев), занижать её (буфер <1.0)
# означало бы разрешать отставание даже от худшего наблюдённого сценария —
# гейт перестанет ловить провал. Обоснование: спека §8.2.
_PLAN_GATE_BUFFER = 1.0

# --- План-гейт v3 (Шаг A.3): пороги прогнозного движка budget-context ---
# Сигнал = forecast_eom vs plan (правило движка), НЕ fact_mtd vs plan.
_ENGINE_FORECAST_K = 0.90   # блок только если прогноз EOM < плана × 0.90 (проектируемый недобор >10%)
_ENGINE_PACE_K = 0.90       # ...И темп pace_vs_expected < 0.90 (даже с учётом сезонности отстаём)
_ENGINE_WAPE_MAX = 0.25     # WAPE движка по revenue_new/new_sales > 25% → не доверяем, откат на кривую
_ENGINE_STALE_HOURS = 36    # data_as_of старше 36ч → не доверяем (пересчёт к 09:30, крон в 13:xx)


def compute_cdp_unit_economics(now: datetime) -> dict | None:
    """Считает юнитку (ДРР + сезонный план-темп) из CDP для гейтов Бюджет-пилота
    (§6.2, §10.2 спеки ARCH-cdp-unit-economics + §6.1-6.2 ARCH-cdp-seasonal-pacing).

    Окно ДРР — то же compute_drr_window(now) (субботнее правило сохраняется).
    ДРР = sum(ad_spend_usd_i * usd_rate_i) / sum(revenue_new_lcy_i) по ВСЕМ
    городам и дням trailing-окна (агрегация сумм, а не среднее готовых drr_new —
    иначе маленький город с одной случайной оплатой исказит вес, см. §10.2).

    Сезонный план-гейт (Шаг A.2): fact_share = факт/план выручки месяца по всем
    городам, expected_share = pacing_curve.expected_cumulative_share(now) —
    историческая консервативная кумулятивная доля к сегодняшнему дню. Подъём
    разрешён если fact_share >= expected_share * _PLAN_GATE_BUFFER. Дни 1-4
    месяца (expected_share=0.0) — гейт нейтрален (нет статбазы). time_pct
    сохраняется в результате справочно (дайджест/лог), но на решение больше
    не влияет.

    При ЛЮБОЙ ошибке CDP (CdpError) → возвращает None, вызывающий делает
    fallback на Google-лист. Исключение НИКОГДА не пробрасывается наружу.

    Returns:
        dict (см. §6.2) или None, если CDP недоступен.
    """
    try:
        drr_start, drr_end, drr_window_end = compute_drr_window(now)

        # --- ДРР: сумма расход/сумма выручка по trailing-окну (§10.2) ---
        items = cdp_client.get_daily_report(drr_start.date(), drr_end.date())
        spend_lcy = sum(float(it.get("ad_spend") or 0) * float(it.get("usd_rate") or 0) for it in items)
        revenue_lcy = sum(float(it.get("revenue_new") or 0) for it in items)
        spend_usd = sum(float(it.get("ad_spend") or 0) for it in items)
        drr = (spend_lcy / revenue_lcy) if revenue_lcy > 0 else None

        # --- Сезонный план-гейт: факт/план выручки текущего месяца (§6.2) ---
        summary = cdp_client.get_plan_fact_summary(now.date())
        cities = summary.get("cities") or []
        # (c.get("plan") or {}) — а НЕ c.get("plan", {}): дефолт .get() срабатывает
        # только при ОТСУТСТВИИ ключа, а CDP может отдать явный "plan": null
        # (регрессия). Город без плана/факта просто не даёт вклада в сумму (0).
        # isinstance(c, dict) — элементы cities тоже могут оказаться не-dict/None.
        plan_rev = sum(
            float((c.get("plan") or {}).get("revenue_new") or 0)
            for c in cities if isinstance(c, dict)
        )
        fact_rev = sum(
            float((c.get("fact") or {}).get("revenue_new") or 0)
            for c in cities if isinstance(c, dict)
        )

        # time_pct сохраняем в результате как справочную величину (дайджест/лог),
        # но на РЕШЕНИЕ гейта он больше не влияет.
        time_pct = float(summary.get("time_pct") or 0)
        if time_pct > 1:  # CDP может отдать проценты (33.0) вместо доли (0.33)
            time_pct /= 100.0

        # fact_share — доля выполнения МЕСЯЧНОГО плана выручки по факту.
        fact_share = (fact_rev / plan_rev) if plan_rev > 0 else None
        # expected_share — ожидаемая кумулятивная доля к сегодняшнему дню
        # (историческая консервативная кривая). Дни 1-4 → 0.0 (гейт нейтрален).
        expected_share = pacing_curve.expected_cumulative_share(now)

        if fact_share is None:
            # Нет планового знаменателя — не выдумываем блокировку по темпу
            # (как в Шаге A). ДРР-гейт остаётся главным предохранителем.
            plan_ok, plan_reason = True, None
        elif expected_share <= 0.0:
            # Дни 1-4: нет статбазы для алертов — гейт нейтрален. Защита
            # остаётся на ДРР-гейте и дневном капе ≤+15% (см. §8 спеки).
            plan_ok, plan_reason = True, None
        elif fact_share >= expected_share * _PLAN_GATE_BUFFER:
            # Факт не отстаёт от исторической консервативной кривой (или
            # перевыполнен — expected_share≤1.0, ветка покрывает и этот случай).
            plan_ok, plan_reason = True, None
        else:
            plan_ok = False
            plan_reason = (
                f"план отстаёт (сезонно): факт {fact_share*100:.0f}% выручки при "
                f"ожидаемых ≥{expected_share*100:.0f}% к {now.day}-му числу — не поднимаем"
            )

        # fact_pace оставляем для обратной совместимости лога/телеметрии (= fact_share)
        fact_pace = fact_share

        return {
            "drr": drr,
            "spend_usd": spend_usd,
            "revenue_lcy": revenue_lcy,
            "drr_window_end": drr_window_end,
            "plan_ok": plan_ok,
            "plan_reason": plan_reason,
            "fact_pace": fact_pace,          # = fact_share (обратная совместимость)
            "time_pct": time_pct,            # справочная величина, на решение не влияет
            # --- Сезонный план-гейт (Шаг A.2) ---
            "plan_gate_mode": "seasonal",
            "expected_share": expected_share,
            "fact_share": fact_share,
        }
    except CdpError as exc:
        logger.warning("compute_cdp_unit_economics: CDP недоступен — %s", exc)
        return None


# ---------------------------------------------------------------------------
# Прогнозный движок budget-context — план-гейт v3 (Шаг A.3, ARCH-cdp-budget-context §6.3)
# ---------------------------------------------------------------------------

def _fnum(x) -> float | None:
    """float(x) или None (None/'' → None, битое → None)."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _engine_metric(ctx: dict, name: str) -> dict | None:
    """metrics[name] агрегата city=='_total'. None если нет _total или метрики.

    Элементы cities могут оказаться не-dict/None (битые данные CDP) —
    isinstance-фильтр не даёт им уронить прогон.
    """
    for c in (ctx.get("cities") or []):
        if not isinstance(c, dict):
            continue
        if c.get("city") == "_total":
            m = (c.get("metrics") or {}).get(name)
            return m if isinstance(m, dict) else None
    return None


def _engine_wape(ctx: dict, metric: str) -> float | None:
    """Последний (по target_month) wape метрики из engine_accuracy. None если нет."""
    rows = [
        r for r in (ctx.get("engine_accuracy") or [])
        if isinstance(r, dict) and r.get("metric") == metric
    ]
    if not rows:
        return None
    # берём самый свежий target_month (строки сравнимы лексикографически для YYYY-MM)
    row = max(rows, key=lambda r: str(r.get("target_month") or ""))
    return _fnum(row.get("wape"))


def _parse_data_as_of(s) -> datetime | None:
    """ISO8601 'YYYY-MM-DDTHH:MM:SSZ' → aware datetime UTC. None если битое."""
    if not s or not isinstance(s, str):
        return None
    try:
        # 'Z' → '+00:00' для fromisoformat (py<3.11 не ест 'Z')
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _engine_distrust_reason(ctx: dict, now: datetime) -> str | None:
    """None = движку доверяем. Иначе — короткая ЧЕЛОВЕЧЕСКАЯ причина недоверия
    (без аббревиатур вроде WAPE — текст для нетехнического читателя).
    Логика (какие условия → недоверие) НЕ изменена, изменён только текст."""
    if ctx.get("is_cold_start") is True:
        return "прогноз CDP ещё не готов (холодный старт месяца)"
    w_rev = _engine_wape(ctx, "revenue_new")
    w_sales = _engine_wape(ctx, "new_sales")
    if w_rev is not None and w_rev > _ENGINE_WAPE_MAX:
        return (
            f"прогноз CDP по выручке в этом месяце ошибается на {w_rev * 100:.0f}% "
            f"(допустимо {_ENGINE_WAPE_MAX * 100:.0f}%)"
        )
    if w_sales is not None and w_sales > _ENGINE_WAPE_MAX:
        return (
            f"прогноз CDP по продажам в этом месяце ошибается на {w_sales * 100:.0f}% "
            f"(допустимо {_ENGINE_WAPE_MAX * 100:.0f}%)"
        )
    as_of = _parse_data_as_of(ctx.get("data_as_of"))
    if as_of is None:
        return "у прогноза CDP нет отметки свежести"
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    age_hours = (now_utc - as_of).total_seconds() / 3600.0
    if age_hours > _ENGINE_STALE_HOURS:
        return (
            f"данные прогноза CDP устарели на {age_hours:.0f}ч "
            f"(допустимо {_ENGINE_STALE_HOURS}ч)"
        )
    return None


def _fmt_money(x: float | None) -> str:
    """Человекочитаемая сумма в миллионах ('n/a' если None)."""
    return f"{x/1_000_000:.1f}M" if x is not None else "n/a"


def _fmt_pace(x: float | None) -> str:
    """Человекочитаемый темп с 2 знаками ('n/a' если None)."""
    return f"{x:.2f}" if x is not None else "n/a"


def compute_engine_pace(now: datetime) -> dict | None:
    """Считает темп плана и тормоз перерасхода из прогнозного движка CDP
    (GET /analytics/budget-context, агрегат city='_total') для план-гейта v3
    Бюджет-пилота (§6.3 спеки ARCH-cdp-budget-context).

    Сигнал ТЕМПА = forecast_eom vs plan (НЕ fact_mtd vs plan) — правило движка:
    оплаты запаздывают, «сегодня мало оплат» само по себе НЕ повод блокировать.

    Доверие движку (все условия обязательны, иначе → None + причина недоверия
    считается ОТДЕЛЬНО через _engine_distrust_reason для триггера сомнений):
      - is_cold_start == False
      - wape(revenue_new) <= _ENGINE_WAPE_MAX (0.25)
      - wape(new_sales)   <= _ENGINE_WAPE_MAX (0.25)
      - data_as_of не старше _ENGINE_STALE_HOURS (36) от now

    План-гейт (revenue_new._total): plan_ok=True если
        status in {"ahead","on_track","no_plan"}
        OR forecast_eom >= plan * _ENGINE_FORECAST_K
        OR pace_vs_expected >= _ENGINE_PACE_K.
    Блок (plan_ok=False) только если status=="behind" И forecast_eom < plan*K
        И pace_vs_expected < _ENGINE_PACE_K.

    Тормоз перерасхода (ad_spend._total, cost-метрика — status инвертирован):
        overspend_block=True если status=="behind" И plan is not None (не no_plan).

    При ЛЮБОЙ ошибке CDP (CdpError) ИЛИ невалидной форме (нет _total / нет
    revenue_new) → возвращает None (fallback на сезонную кривую). Исключение
    НИКОГДА не пробрасывается наружу.

    Returns:
        dict | None (см. §6.3 спеки) или None (недоверие/ошибка/нет данных).
    """
    try:
        ctx = cdp_client.get_budget_context()  # агрегат _total уже внутри
    except CdpError as exc:
        logger.warning("compute_engine_pace: budget-context недоступен — %s", exc)
        return None
    except Exception as exc:  # двойная страховка — движок не роняет прогон
        logger.warning("compute_engine_pace: неожиданная ошибка budget-context — %s", exc)
        return None

    # --- доверие движку ---
    distrust = _engine_distrust_reason(ctx, now)  # str или None
    if distrust is not None:
        logger.info("compute_engine_pace: движку не доверяем — %s", distrust)
        return None  # откат на кривую (недоверие сигналится отдельно, см. §6.6)

    rev = _engine_metric(ctx, "revenue_new")   # dict или None
    spend = _engine_metric(ctx, "ad_spend")    # dict или None
    if rev is None:
        logger.info("compute_engine_pace: нет revenue_new._total — откат на кривую")
        return None

    rev_status = rev.get("status")
    plan = _fnum(rev.get("plan"))
    forecast = _fnum(rev.get("forecast_eom"))
    pace = _fnum(rev.get("pace_vs_expected"))

    # --- план-гейт: сигнал = forecast_eom vs plan (не fact_mtd) ---
    if rev_status in ("ahead", "on_track", "no_plan") or plan is None:
        plan_ok, plan_reason = True, None
    else:
        forecast_ok = forecast is not None and forecast >= plan * _ENGINE_FORECAST_K
        pace_ok = pace is not None and pace >= _ENGINE_PACE_K
        if forecast_ok or pace_ok:
            plan_ok, plan_reason = True, None
        else:
            plan_ok = False
            plan_reason = (
                f"движок: прогноз выручки {_fmt_money(forecast)} < плана "
                f"{_fmt_money(plan)} к концу месяца (темп {_fmt_pace(pace)}) — не поднимаем"
            )

    # --- тормоз перерасхода: cost-метрика, status инвертирован (используем НАПРЯМУЮ) ---
    overspend_block, overspend_reason = False, None
    if spend is not None:
        spend_status = spend.get("status")
        spend_plan = _fnum(spend.get("plan"))
        # только когда план есть (no_plan => plan is None => пропускаем)
        if spend_status == "behind" and spend_plan is not None:
            overspend_block = True
            overspend_reason = (
                f"движок прогнозирует перерасход бюджета (>115% плана, статус behind, "
                f"прогноз ${_fmt_money(_fnum(spend.get('forecast_eom')))}) — не поднимаем"
            )
    else:
        spend_status = None

    return {
        "plan_ok": plan_ok,
        "plan_reason": plan_reason,
        "overspend_block": overspend_block,
        "overspend_reason": overspend_reason,
        "forecast_eom": forecast,
        "pace_vs_expected": pace,
        "engine_wape": _engine_wape(ctx, "revenue_new"),
        "revenue_status": rev_status,
        "spend_status": spend_status,
        "engine_distrust_reason": None,
    }


def _engine_distrust_for_doubt(now: datetime) -> str | None:
    """Тонкая fail-safe обёртка над _engine_distrust_reason для триггера
    сомнений (д) (§6.6 спеки). compute_engine_pace при недоверии возвращает
    None и «глотает» причину — эта функция достаёт причину ОТДЕЛЬНО, не
    завися от того, что вызывает основной путь.

    Кеш budget-context (TTL 3600) гарантирует, что повторный get_budget_context()
    здесь НЕ делает второй сетевой запрос в рамках одного прогона.

    Returns:
        str | None: причина недоверия или None (движок доверенный/недоступен —
        в последнем случае триггер (д) просто не сработает, это ОК, т.к.
        основной путь и так откатится на кривую).
    """
    try:
        ctx = cdp_client.get_budget_context()
        return _engine_distrust_reason(ctx, now)
    except Exception as exc:
        logger.info("_engine_distrust_for_doubt: budget-context недоступен — %s", exc)
        return None


# ---------------------------------------------------------------------------
# Дефолтные значения конфига (добавляются в AUTOPILOT_DEFAULTS через mixin)
# ---------------------------------------------------------------------------

SCALE_DEFAULTS = {
    # Мастер-ключ: FALSE по умолчанию — только рекомендации
    "scale_enabled": False,
    # Максимальный процент роста бюджета В СУТКИ на адсет (не за прогон!)
    "max_budget_increase_pct": 15,
    # Потолок-множитель: нельзя поднять выше X × бюджет начала дня
    "max_adset_budget_mult": 2.0,
    # Абсолютный потолок бюджета одного адсета (в центах × 100 = долларах)
    # Дефолт: $300
    "max_adset_daily_budget": 300,
    # Потолок суммарного дневного бюджета всех активных адсетов ($)
    # Дефолт: $4000
    "max_total_daily_budget": 4000,
    # Максимум адсетов за один прогон
    "max_scales_per_run": 2,
    # --- Протокол сомнений (Шаг A.2) — пороги детерминированных триггеров.
    # Технические константы (не бизнес-настройка); флаг вкл/выкл — cdp.doubt_alerts.
    "doubt_divergence_pp": 3.0,        # (а) расхождение ДРР источников > N п.п. → сомнение
    "doubt_drr_near_target_rel": 0.20, # (б) |ДРР−цель| ≤ 20% относительных от цели → сомнение
    "doubt_plan_edge_rel": 0.10,       # (в) факт в пределах ±10% от границы план-гейта → сомнение
}

# Отбор кандидатов по факту продаж (см. _select_sales_candidates)
_MIN_DAYS_RUNNING_FOR_SCALE = 3  # адсеты, где все объявления моложе — в learning-фазе


def _sales_window_label() -> str:
    """Честная подпись окна сверки оплат для текста Telegram.

    Раньше тексты писали «за 7 дн» — косметика: реальное окно сверки AMO ≈2 мес
    (amo_outcomes.OUTCOMES_MONTHS_BACK). Это НЕ SQL-фильтр, только человеческая
    подпись (фикс честности окна)."""
    return "≈2 мес (окно сверки AMO)"


# ---------------------------------------------------------------------------
# Получение конфига масштабирования
# ---------------------------------------------------------------------------

def get_scale_config() -> dict:
    """Читает конфиг автопилота и мержит поверх SCALE_DEFAULTS.

    Возвращает объединённый конфиг: SCALE_DEFAULTS + autopilot settings.
    """
    from services.autopilot import get_autopilot_config
    autopilot_cfg = get_autopilot_config()
    # SCALE_DEFAULTS — база, autopilot_cfg перезаписывает
    cfg = {**SCALE_DEFAULTS, **autopilot_cfg}
    return cfg


# ---------------------------------------------------------------------------
# State-файл: кулдаун между реальными повышениями
# ---------------------------------------------------------------------------

def _load_scale_state() -> dict:
    """Загружает state кулдауна из файла. При ошибке — дефолт."""
    default: dict = {"last_scaled_at": None}
    if not _SCALE_STATE_FILE.exists():
        return default
    try:
        data = json.loads(_SCALE_STATE_FILE.read_text(encoding="utf-8"))
        default.update(data)
        return default
    except Exception as exc:
        logger.warning("_load_scale_state: не удалось прочитать — %s", exc)
        return default


def _save_scale_state(state: dict) -> None:
    """Атомарно сохраняет state (tmp + rename)."""
    _SCALE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SCALE_STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_SCALE_STATE_FILE)
    except Exception as exc:
        logger.error("_save_scale_state: не удалось сохранить — %s", exc)
        raise


def _is_in_cooldown() -> tuple[bool, str | None]:
    """Проверяет, действует ли кулдаун между подъёмами.

    Approval-first: отсечка ставится в момент ОТПРАВКИ предложения владельцу
    (см. _record_scaled_at) — иначе крон каждые полчаса просил бы одно и то же.
    Проекция SCALE_STATE по-прежнему учитывается: если подъём уже исполнен
    execution boundary, её timestamp тоже закрывает окно.

    Returns:
        (True, причина) если кулдаун активен, (False, None) если можно работать.
    """
    state = _load_scale_state()
    last_scaled_at = state.get("last_scaled_at")
    try:
        from agent.database import get_action_state_projections

        projections = get_action_state_projections("SCALE_STATE")
        if projections:
            projected_at = projections[-1]["payload"].get("last_scaled_at")
            if projected_at and (not last_scaled_at or projected_at > last_scaled_at):
                last_scaled_at = projected_at
    except RuntimeError:
        pass
    if not last_scaled_at:
        return False, None
    try:
        last_dt = datetime.fromisoformat(last_scaled_at)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=_TZ_LOCAL)
        now = datetime.now(_TZ_LOCAL)
        elapsed_hours = (now - last_dt).total_seconds() / 3600
        if elapsed_hours < _COOLDOWN_HOURS:
            minutes_left = int((_COOLDOWN_HOURS - elapsed_hours) * 60)
            reason = f"cooldown: последнее повышение {elapsed_hours:.1f}ч назад, ещё {minutes_left} мин"
            return True, reason
    except (ValueError, TypeError) as exc:
        logger.warning("_is_in_cooldown: не удалось разобрать last_scaled_at=%s: %s", last_scaled_at, exc)
    return False, None


def _record_scaled_at() -> None:
    """Записывает timestamp последнего запрошенного подъёма (взводит кулдаун).

    Вызывается сразу после создания SCALE-предложения владельцу: сам подъём
    произойдёт позже, в execution boundary, а окно тишины должно начинаться с
    момента просьбы — повторно предлагать то же самое каждый тик запрещено.
    """
    state = _load_scale_state()
    state["last_scaled_at"] = datetime.now(_TZ_LOCAL).isoformat()
    _save_scale_state(state)


# ---------------------------------------------------------------------------
# FB API: чтение бюджетов всего аккаунта
# ---------------------------------------------------------------------------

def _fetch_all_account_adset_budgets() -> dict | None:
    """Запрашивает у FB daily_budget + effective_status для ВСЕХ адсетов аккаунта.

    Используется для точного подсчёта суммарного бюджета при проверке
    max_total_daily_budget — нельзя ограничиваться только кандидатами.

    Пагинация cursor-based (как get_all_ads в fb_common.py).
    Бюджет FB хранит в ЦЕНТАХ, конвертируем в доллары (/ 100).

    Returns:
        {adset_id: {"daily_budget_usd": float, "effective_status": str, "name": str}}
        при полном успехе;
        None — если FB вернул не-200 в ходе пагинации (fail-closed: сумма
        бюджетов неполна, доверять ей нельзя).
        Пустой аккаунт (нет адсетов) — это {} (валидная сумма 0), а не None.
    """
    from agent.fb_common import _throttled_get, API
    from services.fb_token_provider import get_fb_token, get_fb_account_id

    result: dict = {}
    url = f"{API}/act_{get_fb_account_id()}/adsets"
    access_token = get_fb_token()
    cursor_after = None

    while True:
        params: dict = {
            "access_token": access_token,
            "fields": "id,name,daily_budget,effective_status",
            # Берём только активные — для суммирования бюджета нужны именно они
            "effective_status": json.dumps(["ACTIVE"]),
            "limit": 200,
        }
        if cursor_after:
            params["after"] = cursor_after

        try:
            resp = _throttled_get(url, params=params)
        except Exception as exc:
            logger.error("_fetch_all_account_adset_budgets: ошибка запроса: %s", exc)
            raise

        if resp.status_code != 200:
            logger.warning(
                "_fetch_all_account_adset_budgets: FB %d — %s",
                resp.status_code, resp.text[:200],
            )
            # Fail-closed: неполный ответ = сумме бюджетов доверять нельзя.
            # Возвращаем None (маркер сбоя), чтобы вызывающий код НЕ поднял
            # бюджеты в этот прогон вместо тихого подсчёта частичной суммы
            # (денежный fail-open → fail-closed).
            return None

        data = resp.json()
        for adset in data.get("data", []):
            adset_id = adset.get("id", "")
            if not adset_id:
                continue
            budget_cents = int(adset.get("daily_budget") or 0)
            result[adset_id] = {
                "daily_budget_usd": budget_cents / 100.0,
                "effective_status": adset.get("effective_status", "UNKNOWN"),
                "name": adset.get("name", ""),
            }

        paging = data.get("paging", {})
        next_cursor = paging.get("cursors", {}).get("after")
        if not paging.get("next") or not next_cursor:
            break
        cursor_after = next_cursor

    logger.info(
        "_fetch_all_account_adset_budgets: получено %d активных адсетов из аккаунта",
        len(result),
    )
    return result


# ---------------------------------------------------------------------------
# FB API: чтение бюджетов по конкретным adset_id
# ---------------------------------------------------------------------------

def _fetch_adset_budgets(adset_ids: list[str]) -> dict:
    """Запрашивает у FB daily_budget + effective_status + name для переданных adset_id.

    Использует batch по 50 id за запрос (аналогично _fetch_candidate_fb_info).
    Бюджет FB хранит в ЦЕНТАХ, мы конвертируем в доллары (/ 100).

    Returns:
        {adset_id: {"daily_budget_usd": float, "effective_status": str, "name": str}}
        При ошибке — пустой dict для этого adset_id.
    """
    if not adset_ids:
        return {}

    from agent.fb_common import _throttled_get, API
    from services.fb_token_provider import get_fb_token

    result: dict = {}
    batch_size = 50
    for i in range(0, len(adset_ids), batch_size):
        chunk = adset_ids[i: i + batch_size]
        ids_param = ",".join(chunk)
        try:
            resp = _throttled_get(
                f"{API}",
                params={
                    "access_token": get_fb_token(),
                    "ids": ids_param,
                    "fields": "id,name,daily_budget,effective_status",
                },
            )
            if resp.status_code != 200:
                logger.warning(
                    "_fetch_adset_budgets: FB вернул %d для %d adset_id",
                    resp.status_code, len(chunk),
                )
                continue
            data = resp.json()
            for adset_id, info in data.items():
                # daily_budget в центах — делим на 100 для долларов
                budget_cents = int(info.get("daily_budget") or 0)
                result[adset_id] = {
                    "daily_budget_usd": budget_cents / 100.0,
                    "effective_status": info.get("effective_status", "UNKNOWN"),
                    "name": info.get("name", ""),
                }
        except Exception as exc:
            logger.warning("_fetch_adset_budgets: ошибка для chunk[%d]: %s", i, exc)
    return result


def set_adset_budget(adset_id: str, new_budget_usd: float) -> bool:
    """Legacy boundary: прямое изменение бюджета запрещено."""
    del adset_id, new_budget_usd
    raise RuntimeError("DIRECT_BUDGET_MUTATION_DENIED")


# ---------------------------------------------------------------------------
# Отбор кандидатов по факту продаж (§10.2 спеки Фазы 2)
# ---------------------------------------------------------------------------

def _eff_payments(ad: dict, use_erp_payments: bool) -> int | None:
    """Эффективный сигнал оплат под переключателем cdp.payments_source (§6.5).

    use_erp_payments=False (amo/shadow, дефолт) — старое поведение 1:1:
    ad.get("payments"). use_erp_payments=True (erp) — payments_effective(ad) =
    max(payments_amo, payments_erp), fail-closed (None только если оба None).

    Тот же helper (по смыслу) что и в decision_policy._eff_payments — критично
    держать ОБА модуля синхронными (§8 спеки: рассинхрон Страж↔скейлер).
    """
    if not use_erp_payments:
        return ad.get("payments")
    from services.cdp_payments import payments_effective
    return payments_effective(ad)


def _has_payment(ad: dict, use_erp_payments: bool = False) -> bool:
    """True если у объявления есть подтверждённая оплата (payments>0, не None)."""
    p = _eff_payments(ad, use_erp_payments)
    return p is not None and int(p) > 0


def _is_confirmed_waster(ad: dict, use_erp_payments: bool = False) -> bool:
    """True если объявление — подтверждённый слив.

    Критерий (совместим с decision_policy): сверка была (outcomes_matched_at != NULL)
    И payments == 0 (именно 0, не None). qual/spend не проверяем — для блокировки
    долива в адсет достаточно факта «сверено и оплат ноль».

    use_erp_payments=True (§6.5, §8 спеки): payments == 0 требует, чтобы ОБА
    источника (AMO и ERP) дали 0 (или один 0 + другой None → max=0) — иначе
    payments_effective > 0 и waster=False. Это устраняет рассинхрон со Стражем:
    если Страж (decision_policy) в режиме "erp" не считает объявление сливом
    из-за ERP-оплаты, скейлер тоже не должен блокировать долив по устаревшему AMO=0.
    """
    matched = ad.get("outcomes_matched_at")
    p = _eff_payments(ad, use_erp_payments)
    return matched is not None and p == 0


# Безопасный дефолт порога значимости слива ($). Используется, когда блока
# настроек scaler_v2 ещё нет в конфиге (fail-safe чтение).
_WASTER_MIN_SPEND_DEFAULT_USD = 10.0
# Верхний потолок порога ($). Зеркалирует money-safety-кап settings-API
# (web/settings_validation.py: waster_min_spend_usd 0..1000). Прямая правка
# settings.json обходит API — здесь клампим, чтобы завышенный порог не обошёл
# строгое confirmed_waster-вето (значимый слив всё равно ветит адсет).
_WASTER_MIN_SPEND_MAX_USD = 1000.0


def _waster_min_spend_usd(cfg_v2: dict | None) -> float:
    """Порог значимости слива в USD (autopilot.scaler_v2.waster_min_spend_usd).

    Ниже этого расхода объявление с 0 оплат сливом НЕ считается — это молодое
    или почти не открутившееся объявление, а не реальный слив бюджета. Если
    блока scaler_v2 нет / значение битое → безопасный дефолт $10.
    """
    try:
        val = float((cfg_v2 or {}).get("waster_min_spend_usd", _WASTER_MIN_SPEND_DEFAULT_USD))
    except (TypeError, ValueError):
        return _WASTER_MIN_SPEND_DEFAULT_USD
    # Отрицательный/NaN порог обессмыслил бы вето — откатываемся на безопасный дефолт.
    if not (val >= 0):  # NaN даёт False на любом сравнении → сюда же
        return _WASTER_MIN_SPEND_DEFAULT_USD
    # Верхний кап: прямая правка settings.json обходит settings-API (0..1000),
    # а завышенный порог ослабил бы вето слива. Клампим, а не откатываем на
    # дефолт — так вето остаётся максимально строгим (min из значения и потолка).
    return min(val, _WASTER_MIN_SPEND_MAX_USD)


def _require_fresh_7d(cfg_v2: dict | None) -> bool:
    """Флаг честного 7d-гейта (autopilot.scaler_v2.require_fresh_7d, дефолт False).

    При True active-подъём разрешён ТОЛЬКО если source-specific честное 7d-окно
    оплат СВЕЖЕЕ И ПОЛНОЕ (см. cdp_payments.seven_d_window_confirmed) — иначе
    fail-closed: никакого active raise (stale/partial/unknown → пропуск). Это
    ДОПОЛНИТЕЛЬНЫЙ предохранитель ПОВЕРХ всех инвариантов Фазы 2 (строгое вето
    слива, планы, потолки), а не их замена.

    Дефолт False — поведение по умолчанию НЕ меняется (совместимость с
    утверждёнными инвариантами и существующими тестами; включение honest-7d —
    осознанное решение оператора, «safe default: active mutations не включаются
    автоматически»). Битое/нечисловое значение → False (fail-safe чтение).
    """
    try:
        return bool((cfg_v2 or {}).get("require_fresh_7d", False))
    except (TypeError, ValueError, AttributeError):
        return False


def _is_significant_waster(ad: dict, use_erp_payments: bool, min_spend_usd: float) -> bool:
    """True если объявление — ЗНАЧИМЫЙ подтверждённый слив.

    Значимый слив = confirmed_waster (сверено И effective payments==0, см.
    _is_confirmed_waster) И расход ≥ min_spend_usd. Порог значимости отделяет
    реальный слив (деньги потрачены, оплат ноль) от молодой околонулевой
    нулёвки (расход <порога) — последняя адсет НЕ блокирует.
    """
    if not _is_confirmed_waster(ad, use_erp_payments=use_erp_payments):
        return False
    return float(ad.get("spend") or 0.0) >= min_spend_usd


def _adset_waster_veto(
    adset_ads: list[dict], use_erp_payments: bool, cfg_v2: dict
) -> tuple[bool, str | None]:
    """СТРОГОЕ all-or-nothing вето слива на уровне адсета (инвариант Фазы 2).

    ОДИН значимый слив внутри адсета (any-вето) исключает ВЕСЬ адсет из подъёма —
    никакого «долива в слив», даже если рядом сильные победители. Это НЕ доля /
    процент / большинство — one-strike сохранён строго.

    Уточнение порога значимости: «значимый» = confirmed_waster И
    расход ≥ waster_min_spend_usd (дефолт $10). Смысл — молодое объявление с
    околонулевым расходом (<$10, оплат 0) не должно рубить весь адсет, но ОДИН
    реальный слив (расход ≥$10, оплат 0) блокирует адсет целиком. Порог значимости
    относится к САМОМУ сливу, а не к доле расхода адсета.

    Работает для любого payments_source (amo/shadow/erp): семантику effective
    payments определяет _is_confirmed_waster через use_erp_payments (в erp-режиме
    payments==0 требует, чтобы ОБА источника дали 0 — устраняет рассинхрон со
    Стражем). payments is None НЕ считается confirmed zero.

    Никакой config-флаг НЕ ослабляет это вето (требование Wave 3B плана ревью):
    из настроек читается только порог значимости самого слива, не сам факт вето.

    Returns:
        (veto: bool, reason: str|None). reason заполнен только при veto=True.
    """
    min_spend = _waster_min_spend_usd(cfg_v2)
    if any(_is_significant_waster(a, use_erp_payments, min_spend) for a in adset_ads):
        reason = (
            f"есть значимый confirmed_waster внутри (сверено, оплат 0, "
            f"расход ≥${min_spend:.0f}) — весь адсет исключён (all-or-nothing)"
        )
        return True, reason
    return False, None


def _select_sales_candidates(local_ads: list[dict], use_erp_payments: bool = False) -> list[dict]:
    """Отбирает объявления-кандидатов по ФАКТУ ПРОДАЖ (не по CPL/qual/romi).

    Возвращает список объявлений (dict как в local_ads), отсортированный
    по силе сигнала продаж: payments↓, qual_pct↓, romi↓.
    Адсетную группировку/агрегацию (waster/learning-гейты) делает вызывающий
    код ПОСЛЕ добора adset_id — здесь фильтруем только на уровне объявлений:
    только те, у кого есть подтверждённая оплата (_has_payment).

    use_erp_payments — источник-каскад оплат (§6.5): False (amo/shadow) — старое
    поведение; True (erp) — payments_effective(ad) = max(AMO, ERP).
    """
    winners = [ad for ad in local_ads if _has_payment(ad, use_erp_payments)]
    winners.sort(
        key=lambda a: (
            int(_eff_payments(a, use_erp_payments) or 0),
            float(a.get("qual_pct") or 0),
            float(a.get("romi") or 0),
        ),
        reverse=True,
    )
    return winners


def _calc_new_budget(
    current_budget_usd: float,
    start_budget_usd: float,
    effective_pct: float,
    max_mult: float,
    max_abs_usd: float,
) -> float:
    """Вычисляет новый бюджет адсета с учётом дневного капа и потолков.

    ИНВАРИНАТ (§10.1 спеки): прибавка в долларах = effective_pct% ОТ БЮДЖЕТА
    НА НАЧАЛО ДНЯ (start_budget_usd), добавляется к ТЕКУЩЕМУ бюджету
    (current_budget_usd). Так суммарный рост за день ограничен ровно
    daily_cap_pct % от start_budget, даже при нескольких подъёмах за сутки.
    Пример: start=$100. Подъём1 +10% → $110 (raised=10%). Подъём2
    effective=min(15,5)=5% от start=$100=+$5 → $115 (ровно +15% от старта).

    Алгоритм:
    1. add_usd = start_budget_usd × (effective_pct/100)
    2. target = current_budget_usd + add_usd
    3. Обрезаем до max_mult × start_budget_usd (потолок-множитель ОТ СТАРТА —
       защита от разгона за день суммой подъёмов)
    4. Обрезаем до max_abs_usd (абсолютный потолок одного адсета)
    5. Никогда не опускаем ниже current_budget_usd (потолки не должны
       давать new < current)
    6. Округляем до 2 знаков

    Returns:
        Новый бюджет. Если потолки уже достигнуты — вернёт current_budget_usd.
    """
    # Шаг 1-2: прибавка = effective_pct% от бюджета начала дня, к текущему
    add_usd = start_budget_usd * (effective_pct / 100.0)
    target = current_budget_usd + add_usd

    # Шаг 3: потолок-множитель считаем ОТ START (защита от разгона за день)
    target = min(target, start_budget_usd * max_mult)

    # Шаг 4: не выше абсолютного потолка
    target = min(target, max_abs_usd)

    # Шаг 5: никогда не опускаем ниже текущего
    target = max(target, current_budget_usd)

    return round(target, 2)


# ---------------------------------------------------------------------------
# Вспомогательные форматтеры для Telegram-сообщений
# ---------------------------------------------------------------------------

def _build_plan_header(
    plan_data: dict | None,
    fb_week_usd: float,
    google_week_usd: float,
    revenue_week_lcy: float,      # выручка trailing-окна ДРР (не планового окна)
    headroom_usd: float | None,
    actual_drr: float | None,
    drr_window_end: datetime | None = None,  # дата субботы окна ДРР (для label)
) -> str:
    """Формирует строку-заголовок с планом/фактом/юниткой для Telegram.

    Returns:
        Строка вида «неделя N (даты) · план $X · факт FB $A + Google $B = $C...»
        Если plan_data=None — пустая строка (план не настроен).
    """
    if not plan_data:
        return ""

    week_label = plan_data.get("week_label", "неизвестно")
    weekly_budget_usd = plan_data.get("budget_usd", 0.0)
    unit_target = plan_data.get("unit_target", 0.0)
    total_spend = fb_week_usd + google_week_usd

    headroom_str = fmt_money(headroom_usd, "$") if headroom_usd is not None else "n/a"
    drr_fact_str = f"{actual_drr*100:.1f}%" if actual_drr is not None else "n/a"
    drr_window_str = drr_window_end.strftime("%d.%m") if drr_window_end is not None else "—"

    return (
        f"📅 {week_label} · план {fmt_money(weekly_budget_usd, '$')}\n"
        f"Факт: FB {fmt_money(fb_week_usd, '$')} + Google {fmt_money(google_week_usd, '$')}"
        f" = {fmt_money(total_spend, '$')} · Запас {headroom_str}\n"
        f"Юнитка: факт {drr_fact_str} / план {unit_target*100:.1f}% · "
        f"ДРР за окно до субботы {drr_window_str}\n"
        f"Выручка (окно ДРР) {fmt_money(revenue_week_lcy, '¤')}\n"
    )


def _pluralize_adsets(n: int) -> str:
    """Русская форма слова «адсет» для числа n: 1 адсет, 2-4 адсета, 5+/11-14 адсетов."""
    n_abs = abs(int(n)) % 100
    n1 = n_abs % 10
    if 11 <= n_abs <= 14:
        return "адсетов"
    if n1 == 1:
        return "адсет"
    if 2 <= n1 <= 4:
        return "адсета"
    return "адсетов"


def _fmt_drr_signature(
    actual_drr: float | None,
    drr_window_end: datetime | None,
    unit_target: float | None,
    google_missing_days: int = 0,
) -> str:
    """Строка ДРР с ЯВНОЙ подписью (чтобы читатель видел, «чей» это ДРР):
    значение · «общий по кабинету (все города)» · окно trailing-7дн · цель ≤N%.

    Окно берём из drr_window_end (суббота — конец окна): начало = end − 6 дней
    (compute_drr_window строит ровно 7 дат до включительно этой субботы).

    google_missing_days > 0 → в хвост подписи добавляется честная пометка
    «Google неполный за N дн»: окно ДРР
    молча суммирует отсутствующие дни Google как 0, из-за чего расход в
    знаменателе занижается, а неполнота данных была невидима.
    """
    drr_str = f"{actual_drr * 100:.1f}%" if actual_drr is not None else "n/a"
    if drr_window_end is not None:
        start = (drr_window_end - timedelta(days=6)).strftime("%d.%m")
        end = drr_window_end.strftime("%d.%m")
        window_str = f"{start}–{end}"
    else:
        window_str = "—"
    target_str = f"≤{unit_target * 100:.1f}%" if unit_target else "n/a"
    sig = (
        f"ДРР {drr_str} — общий по кабинету (все города), "
        f"окно {window_str}, цель {target_str}"
    )
    if google_missing_days and google_missing_days > 0:
        sig += f" · Google неполный за {google_missing_days} дн"
    return sig


def _fmt_doubts_and_footer(doubts: list[str]) -> str:
    """Хвост финального сообщения: секция «Почему сомневаюсь»
    (если сомнения есть) + подпись автономности. Приклеивается в КОНЕЦ сообщения о
    результате прогона — раньше сомнения уходили ОТДЕЛЬНЫМ сообщением ДО подъёма.
    Пустой список сомнений → только подпись «Делаю сам».
    """
    out = ""
    if doubts:
        out += "\n\n🤔 Почему сомневаюсь:\n" + "\n".join(f"• {html.escape(d)}" for d in doubts)
    out += "\n\nДелаю сам. Если надо иначе — отметь."
    return out


def _fmt_trend_note(vetoed: list[str], shadow_vetoed: list[str]) -> str:
    """Хвост отчёта про тренд недельных когорт (волна 3). "" — сказать нечего.

    Показывает ровно два числа: сколько подъёмов тренд снял и сколько снял бы,
    будь режим active. В shadow-режиме ни один подъём не отменён — это видно
    прямо в тексте, чтобы «тень» не читалась как исполненное действие.
    """
    blocks: list[str] = []
    if vetoed:
        blocks.append(
            f"\n\n📉 Тренд снял подъёмов: {len(vetoed)}\n"
            + "\n".join(f"• {html.escape(item)}" for item in vetoed)
        )
    if shadow_vetoed:
        # Пометку «тень» несёт заголовок — в строках она была бы у каждой,
        # поэтому убираем её оттуда (сам суффикс задан trend_gate, не строкой здесь).
        items = [
            item.replace(trend_gate.SHADOW_SUFFIX, "") for item in shadow_vetoed
        ]
        blocks.append(
            f"\n\n📉 Тренд снял бы подъёмов: {len(items)} (тень: не применено)\n"
            + "\n".join(f"• {html.escape(item)}" for item in items)
        )
    return "".join(blocks)


def _msg_no_raise(short_reason: str, drr_sig: str, doubt_footer: str) -> str:
    """Вариант 2: гейты разрешали подъём, но реально ничего
    не подняли. short_reason — краткая честная причина (нет победителей/потолки/FB).
    """
    return (
        f"📊 <b>Budget Scaler</b>: гейты разрешали подъём, но {short_reason} — "
        f"ничего не поднял\n"
        f"{drr_sig}"
        + doubt_footer
    )


# ---------------------------------------------------------------------------
# Протокол сомнений (Шаг A.2, ARCH-cdp-seasonal-pacing §6.4, §8.4-8.6)
# ---------------------------------------------------------------------------

def _fmt_pct(x: float | None) -> str:
    """Форматирует долю в проценты с 1 знаком, "n/a" для None."""
    return f"{x*100:.1f}%" if x is not None else "n/a"


def _evaluate_doubt_triggers(
    unit_source: str,
    cdp_ue: dict | None,
    drr_cdp: float | None,
    drr_sheet: float | None,
    drr_divergence_pp: float | None,
    unit_target: float | None,
    plan_gate_reason: str | None,
    fallback_happened: bool,
    engine_distrust_reason: str | None,
    engine_revenue_behind_spend_ok: bool,
    thresholds: dict,
) -> list[str]:
    """Детерминированно проверяет триггеры «сомнения» и возвращает список
    коротких человеческих формулировок сработавших (пустой список = сомнений нет).

    НЕ блокирует и НЕ меняет решение — только формирует список для одного
    информационного Telegram-сообщения (§8.5-8.6 спеки ARCH-cdp-seasonal-pacing,
    §6.5 спеки ARCH-cdp-budget-context). Порядок проверки фиксирован:
    (а) divergence, (б) drr_near_target, (в) plan_gate_edge, (г) fallback,
    (д) недоверие движку, (е) выручка отстаёт при спенде в норме. Чистая
    функция: не пишет в БД, не бросает исключений на None-значениях (каждый
    триггер защищён is not None).

    Триггеры (все пороги из thresholds = SCALE_DEFAULTS, §8.4):
      (а) divergence: источники ДРР разошлись сильно —
          drr_divergence_pp is not None AND drr_divergence_pp > doubt_divergence_pp.
      (б) drr_near_target: ДРР близко к цели (±relative) — effective_drr (ДРР
          источника, принявшего решение: drr_cdp если unit_source=='cdp',
          иначе drr_sheet) is not None AND unit_target > 0 AND
          abs(effective_drr - unit_target) <= unit_target * doubt_drr_near_target_rel.
      (в) plan_gate_edge: сезонный план-гейт прошёл/не прошёл впритык —
          только если решал CDP (unit_source=='cdp' и cdp_ue is not None и
          cdp_ue['fact_share'] is not None и cdp_ue['expected_share'] > 0):
          border = cdp_ue['expected_share'] * _PLAN_GATE_BUFFER;
          триггер если abs(cdp_ue['fact_share'] - border) <= border * doubt_plan_edge_rel.
      (г) fallback: CDP лёг и произошёл откат на лист — fallback_happened=True.
      (д) недоверие движку (Шаг A.3): engine_distrust_reason is not None —
          движок недоступен/недоверен, темп решён сезонной кривой.
      (е) выручка отстаёт при спенде в норме (Шаг A.3):
          engine_revenue_behind_spend_ok=True — движок доверенный, revenue_new
          behind, а ad_spend в норме — совет проверить каналы (перекладывание
          бюджета Out of Scope, только сообщение владельцу).

    Returns:
        list[str] — человеческие формулировки сработавших триггеров.
    """
    triggers: list[str] = []

    # (а) расхождение источников ДРР
    doubt_divergence_pp = float(thresholds.get("doubt_divergence_pp", SCALE_DEFAULTS["doubt_divergence_pp"]))
    if drr_divergence_pp is not None and drr_divergence_pp > doubt_divergence_pp:
        triggers.append(
            f"CDP и лист разошлись по ДРР на {drr_divergence_pp:.1f} п.п. (порог {doubt_divergence_pp:.0f})"
        )

    # (б) ДРР близко к цели
    doubt_drr_near_target_rel = float(
        thresholds.get("doubt_drr_near_target_rel", SCALE_DEFAULTS["doubt_drr_near_target_rel"])
    )
    effective_drr = drr_cdp if unit_source == "cdp" else drr_sheet
    if effective_drr is not None and unit_target is not None and unit_target > 0:
        if abs(effective_drr - unit_target) <= unit_target * doubt_drr_near_target_rel:
            triggers.append(
                f"ДРР {effective_drr*100:.1f}% почти равен цели {unit_target*100:.1f}% "
                f"(в пределах {doubt_drr_near_target_rel*100:.0f}%)"
            )

    # (в) план-гейт впритык (только для CDP-источника)
    doubt_plan_edge_rel = float(thresholds.get("doubt_plan_edge_rel", SCALE_DEFAULTS["doubt_plan_edge_rel"]))
    if (
        unit_source == "cdp"
        and cdp_ue is not None
        and cdp_ue.get("fact_share") is not None
        and (cdp_ue.get("expected_share") or 0) > 0
    ):
        fact_share = cdp_ue["fact_share"]
        border = cdp_ue["expected_share"] * _PLAN_GATE_BUFFER
        if abs(fact_share - border) <= border * doubt_plan_edge_rel:
            plan_ok_edge = fact_share >= border
            triggers.append(
                f"факт {fact_share*100:.0f}% у самой границы плана {border*100:.0f}% "
                f"({'прошёл' if plan_ok_edge else 'не прошёл'} впритык)"
            )

    # (г) fallback CDP → лист
    if fallback_happened:
        triggers.append("CDP лёг — считаю юнитку по Google-листу")

    # (д) недоверие движку → откат на сезонную кривую (Шаг A.3)
    # Человеческая формулировка без слов «движок»/«WAPE».
    if engine_distrust_reason is not None:
        triggers.append(
            f"{engine_distrust_reason} — не доверяю ему, считаю по сезонной кривой"
        )

    # (е) выручка отстаёт, а спенд в норме → канал, а не общий бюджет (Шаг A.3)
    if engine_revenue_behind_spend_ok:
        triggers.append(
            "выручка отстаёт, а спенд в норме — проверь каналы (CPL/ROMI), возможно переложить бюджет"
        )

    return triggers


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------

def run_budget_scaling(mode: str = "dry_run", max_scales: int = 2) -> dict:
    """Запускает один цикл масштабирования бюджетов.

    Алгоритм:
    1. Читает конфиг и проверяет предохранители (enabled, kill_switch, scale_enabled, кулдаун).
    2. Получает активные объявления из локальной БД.
    3. Прогоняет через _select_sales_candidates → отбирает кандидатов по факту
       продаж (payments>0), отсортированных по силе сигнала.
    4. Получает adset_id ВСЕХ активных объявлений из FB через _fetch_candidate_fb_info
       (не только кандидатов — нужно для групповых защит waster/learning, §10.2).
    5a. Получает бюджеты ВСЕХ активных адсетов аккаунта (для точного суммарного бюджета).
    5b. Дополняет бюджетами кандидатов (если их нет в общем списке).
    6. Проверяет общий потолок (max_total_daily_budget) по сумме ВСЕГО аккаунта.
       Каждое повышение накопительно учитывается в текущем total.
    7. dry_run → рекомендации в Telegram, без изменений.
       active (только если scale_enabled=True) → поднимает daily_budget + записывает кулдаун.

    Args:
        mode: "dry_run" (только рекомендации) или "active" (реальные изменения).
              В режиме active ТРЕБУЕТСЯ scale_enabled=True в конфиге.
        max_scales: максимум адсетов за прогон (перекрывает max_scales_per_run из конфига).

    Returns:
        {
            "ran": bool,
            "skipped_reason": str | None,
            "mode": str,
            "winners": [...],       # объявления-победители
            "recommendations": [...], # что рекомендуется
            "scaled": [...],        # реально изменённые адсеты (только в active)
            "errors": [...],
        }
    """
    # Wave 1B.1: замок ОДНОГО активного прогона. Только для active — dry_run не
    # мутирует FB и не должен конкурировать за mutation-lock (его локальные
    # state-writes всё равно защищены file-lock'ом внутри budget_daily_cap).
    active_lock: "_ActiveRunLock | None" = None
    if mode == "active":
        active_lock = _try_acquire_active_run_lock()
        if active_lock is None:
            logger.info(
                "Budget Scaler: active-прогон уже идёт (или замок занят) — пропускаем (concurrency guard)"
            )
            return _normalized_result({
                "ran": False,
                "skipped_reason": "active_run_in_progress",
                "mode": mode,
                "winners": [],
                "recommendations": [],
                "scaled": [],
                "errors": [],
                **_telemetry_defaults(),
            })

    try:
        return _normalized_result(_run_scaling_inner(mode=mode, max_scales=max_scales))
    except Exception as exc:
        logger.exception("run_budget_scaling: критическая ошибка: %s", exc)
        try:
            from services.notifications import send_critical_alert
            send_critical_alert(
                "Budget Scaler: критическая ошибка",
                str(exc),
            )
        except Exception:
            pass
        return _normalized_result({
            "ran": False,
            "skipped_reason": f"error: {exc}",
            "mode": mode,
            "winners": [],
            "recommendations": [],
            "scaled": [],
            "errors": [str(exc)],
            **_telemetry_defaults(),
        })
    finally:
        if active_lock is not None:
            active_lock.release()


def _normalized_result(result: dict) -> dict:
    """Единая форма результата прогона: ключ ``proposals`` есть у ЛЮБОГО выхода.

    Ранние выходы (гейты enabled/kill_switch/кулдаун/план-гейт/нет кандидатов)
    собирали словарь без ``proposals``, и вызывающий код (`result["proposals"]`)
    падал на KeyError вместо честного «предложений нет». Нормализуем здесь, в
    единственной публичной точке входа, чтобы ни один будущий ранний return не
    потерял счётчик снова.
    """
    if not isinstance(result, dict):
        return result
    result.setdefault("proposals", [])
    return result


def _telemetry_defaults() -> dict:
    """Нейтральная телеметрия CDP-юнитки для ранних return (§6.3): источник
    ещё не определялся (гейты enabled/kill_switch/cooldown отработали ДО
    расчёта юнитки), поэтому unit_source="none", остальное None."""
    return {
        "unit_source": "none",
        "drr_cdp": None,
        "drr_sheet": None,
        "drr_divergence_pp": None,
        "plan_gate_mode": "none",   # Шаг A.2
        "expected_share": None,     # Шаг A.2
        "fact_share": None,         # Шаг A.2
        # --- Шаг A.3: план-гейт v3 (прогнозный движок budget-context) ---
        "pace_source": "none",
        "forecast_eom": None,
        "pace_vs_expected": None,
        "engine_wape": None,
        # Неполнота Google в окне ДРР
        "google_missing_days": 0,
        # --- Этап 4: самопроверка прогноза + sanity-стоп-кран ---
        "selfcheck_dev": None,
        "sanity_cap_hit": False,
        # --- Wave 3C: статус fail-closed гейта самопроверки ---
        "selfcheck_status": "none",
        # --- Волна 3 когорт: тренд. Ранние гейты отработали ДО чтения когорт,
        # поэтому режим ещё не определялся ("none"), а счётчики нулевые.
        "trend_mode": "none",
        "trend_vetoed": 0,          # подъёмов реально ветировано трендом
        "trend_shadow_vetoed": 0,   # ветировал бы, но режим shadow
    }


def _run_scaling_inner(mode: str, max_scales: int) -> dict:
    """Внутренняя реализация run_budget_scaling (без try/except верхнего уровня)."""
    from services.notifications import send_telegram, send_critical_alert

    # --- Шаг 1: конфиг и предохранители ---
    cfg = get_scale_config()

    # ГЕЙТ 1: enabled
    if not cfg.get("enabled"):
        logger.info("Budget Scaler: autopilot.enabled=false — пропускаем")
        return {
            "ran": False, "skipped_reason": "disabled",
            "mode": mode, "winners": [], "recommendations": [], "scaled": [], "errors": [],
            **_telemetry_defaults(),
        }

    # ГЕЙТ 2: kill_switch
    if cfg.get("kill_switch"):
        logger.info("Budget Scaler: kill_switch=true — пропускаем")
        return {
            "ran": False, "skipped_reason": "kill_switch",
            "mode": mode, "winners": [], "recommendations": [], "scaled": [], "errors": [],
            **_telemetry_defaults(),
        }

    # ГЕЙТ 3: в active-режиме требуется scale_enabled
    if mode == "active" and not cfg.get("scale_enabled"):
        logger.info("Budget Scaler: mode=active но scale_enabled=false — переходим в dry_run")
        mode = "dry_run"

    # ГЕЙТ 4: кулдаун — только для active-режима.
    # Dry-run не ограничивается — это безопасное чтение рекомендаций.
    if mode == "active":
        in_cooldown, cooldown_reason = _is_in_cooldown()
        if in_cooldown:
            logger.info("Budget Scaler: %s — пропускаем active", cooldown_reason)
            return {
                "ran": False, "skipped_reason": cooldown_reason,
                "mode": mode, "winners": [], "recommendations": [], "scaled": [], "errors": [],
                **_telemetry_defaults(),
            }

    # Читаем лимиты из конфига
    max_increase_pct = float(cfg.get("max_budget_increase_pct", SCALE_DEFAULTS["max_budget_increase_pct"]))
    max_mult = float(cfg.get("max_adset_budget_mult", SCALE_DEFAULTS["max_adset_budget_mult"]))
    max_abs_usd = float(cfg.get("max_adset_daily_budget", SCALE_DEFAULTS["max_adset_daily_budget"]))
    max_total_usd = float(cfg.get("max_total_daily_budget", SCALE_DEFAULTS["max_total_daily_budget"]))
    cfg_cap = int(cfg.get("max_scales_per_run", SCALE_DEFAULTS["max_scales_per_run"]))
    cap = min(max_scales, cfg_cap)

    # --- ГЕЙТ 5: план+юнитка (недельный бюджет + ДРР) ---
    # Этот гейт определяет допустимость подъёма на уровне всего аккаунта.
    # plan_data — словарь {week_label, budget_usd, revenue_plan_lcy, unit_target}
    #
    # ДВА независимых окна:
    # - Плановое окно (числа месяца) — для headroom-гейта (запас недельного бюджета
    #   по расходу). Расход капает равномерно, выручка тут не участвует — окно честное.
    # - Trailing-окно ДРР (7 полных дней до последней прошедшей субботы) — для
    #   юнитка-гейта (факт ДРР = расход/выручка). Выручка (оплаты AMO) концентрируется
    #   в субботу, поэтому частичное плановое окно до своей субботы завышает ДРР —
    #   считаем по стабильному trailing-окну (см. compute_drr_window).
    plan_data = None
    fb_week_usd = 0.0
    google_week_usd = 0.0
    revenue_week_lcy = 0.0  # выручка trailing-окна ДРР (используется для гейта юнитки)
    headroom_usd = None
    actual_drr = None
    drr_window_end = None  # дата субботы окна ДРР — для label в Telegram
    plan_gate_reason = None  # причина блокировки (или None если разрешено)

    # --- CDP-источник юнитки (§10.4): считаем ВСЕГДА (для shadow-лога/сверки),
    # решение принимает только если cdp.enabled=true и CDP доступен. Любая
    # ошибка CDP ловится внутри compute_cdp_unit_economics — сюда долетает
    # только None, исключение никогда не пробрасывается и не роняет прогон.
    now_local_for_cdp = datetime.now(_TZ_LOCAL)
    cdp_cfg = cfg.get("cdp") or {}
    cfg_v2 = cfg.get("scaler_v2") or {}  # блок настроек Budget Scaler v2 (этапы 1/4)
    # Тренд недельных когорт (волна 3) — читаем ОДИН раз на прогон, до цикла
    # кандидатов. Никогда не бросает: сбой чтения даёт пустой контекст, и вето
    # тренда просто не применяется (см. services/trend_gate.py — здесь НЕ
    # fail-closed намеренно: сбой аналитики не повод блокировать подъём,
    # прошедший все денежные гейты).
    trend_ctx = trend_gate.load_trend_context(cfg=cfg)
    if trend_ctx.error:
        logger.warning(
            "Budget Scaler: тренд недоступен в этом прогоне (%s) — вето тренда не применяется",
            trend_ctx.error,
        )
    cdp_enabled = bool(cdp_cfg.get("enabled"))
    pace_engine_enabled = bool(cdp_cfg.get("pace_engine", True))  # дефолт true (Шаг A.3)
    try:
        cdp_ue = compute_cdp_unit_economics(now_local_for_cdp)
    except Exception as exc:
        # Дополнительная страховка сверх try/except внутри самой функции —
        # CDP-факап не должен ронять прогон Бюджет-пилота ни при каких условиях.
        logger.warning("Budget Scaler: compute_cdp_unit_economics упал неожиданно — %s", exc)
        cdp_ue = None

    unit_source = "sheet"  # источник, ПРИНЯВШИЙ решение (обновится ниже)

    # --- Шаг A.3: слой движка ПОВЕРХ сезонного темпа (§6.6 спеки ARCH-cdp-budget-context) ---
    # engine_pace считаем ДО каскада (сезонная кривая cdp_ue уже посчитана выше — не трогаем).
    # pace_source по умолчанию — "curve", если CDP включён и сезонная кривая доступна,
    # "sheet"/"none" достроятся ниже вместе с остальным каскадом источника юнитки (§6.7).
    engine_pace: dict | None = None
    engine_distrust_reason: str | None = None
    engine_revenue_behind_spend_ok = False
    pace_source = "curve" if (cdp_enabled and cdp_ue is not None) else "none"

    # --- Этап 4: самопроверка прогноза CDP («аккуратно») ---
    # Ежедневный снапшот (forecast_eom/fact_mtd) + недельная сверка «прогноз обещал
    # vs факт» с сезонной поправкой. Сильный недобор факта → недоверие движку
    # (принудительный откат на сезонную кривую) + сомнение владельцу.
    selfcheck_dev: float | None = None
    sanity_cap_hit = False
    selfcheck_reason: str | None = None
    # --- Wave 3C: самопроверка как fail-closed гейт active-подъёма ---
    # Гейт АКТИВЕН когда CDP включён И engine_selfcheck_enabled (дефолт True, Wave 3B).
    # Когда активен — любой сбой самопроверки (недоступность/неполный ответ/битый
    # state/прогрев/ошибка сверки/сильное отставание) НЕ пропускает active-подъём.
    # Когда неактивен (CDP выкл или флаг off) — selfcheck_gate_ok=True (нет блока).
    selfcheck_status: str = "off"
    selfcheck_gate_ok: bool = True
    selfcheck_skip_reason: str | None = None
    engine_selfcheck_enabled = bool(cfg_v2.get("engine_selfcheck_enabled", True))
    sanity_cap_enabled = bool(cfg_v2.get("sanity_cap_enabled", True))
    sanity_cap_ratio = float(cfg_v2.get("sanity_cap_ratio", 0.7))
    selfcheck_max_dev = float(cfg_v2.get("selfcheck_max_dev", 0.25))
    selfcheck_gate_active = cdp_enabled and engine_selfcheck_enabled
    if selfcheck_gate_active:
        try:
            # run_selfcheck сам ловит все ошибки внутри и на любой сбой отдаёт
            # gate_ok=False — прогон не падает, но подъём остаётся заблокированным.
            _sc = engine_selfcheck.run_selfcheck(now_local_for_cdp, selfcheck_max_dev)
            selfcheck_status = _sc.get("status", "unavailable")
            selfcheck_dev = _sc.get("dev")
            selfcheck_reason = _sc.get("reason")
            selfcheck_gate_ok = bool(_sc.get("gate_ok", False))
            selfcheck_skip_reason = _sc.get("skipped_reason")
        except Exception as exc:  # даже неожиданная ошибка оркестратора = fail-closed
            logger.warning("Budget Scaler: engine_selfcheck.run_selfcheck упал неожиданно — %s", exc)
            selfcheck_status = "unavailable"
            selfcheck_gate_ok = False
            selfcheck_skip_reason = "самопроверка недоступна (внутренняя ошибка)"

    if cdp_enabled and pace_engine_enabled:
        try:
            engine_pace = compute_engine_pace(now_local_for_cdp)
        except Exception as exc:  # двойная страховка — движок не роняет прогон
            logger.warning("Budget Scaler: compute_engine_pace упал неожиданно — %s", exc)
            engine_pace = None
        # Этап 4: самопроверка перекрывает движок — принудительно откатываемся на кривую
        if selfcheck_reason is not None:
            engine_pace = None
        if engine_pace is not None:
            pace_source = "engine"
            engine_revenue_behind_spend_ok = (
                engine_pace["revenue_status"] == "behind"
                and engine_pace["spend_status"] in ("on_track", "ahead", "no_plan")
            )
        else:
            # движку не доверяем/недоступен → откат на сезонную кривую (pace_source
            # уже "curve"/"none" выше). Причина: приоритет у самопроверки (Этап 4),
            # иначе WAPE/stale-причина. Триггер сомнений (д), fail-safe (None → нет триггера).
            engine_distrust_reason = selfcheck_reason or _engine_distrust_for_doubt(now_local_for_cdp)

    sheet_id = cfg.get("plan_sheet_id", "")
    if sheet_id:
        try:
            from services.plan_reader import read_general_plan
            now_local = datetime.now(_TZ_LOCAL)
            plan_data = read_general_plan(sheet_id, now=now_local)
        except Exception as exc:
            logger.warning("Budget Scaler: не удалось прочитать план — %s", exc)

        if plan_data:
            # Определяем даты текущей недели по дню месяца (локальная TZ) — плановое окно
            now_local = datetime.now(_TZ_LOCAL)
            day = now_local.day
            year = now_local.year
            month = now_local.month

            # Безопасно строим дату начала/конца недели через date, не replace(day=w_to)
            # (replace падает при w_to > кол-ва дней месяца, напр. июнь 31)
            import calendar
            max_day = calendar.monthrange(year, month)[1]

            week_start_day = 1
            week_end_day = 7
            for w_from, w_to in [(1, 7), (8, 14), (15, 21), (22, 28), (29, 31)]:
                if w_from <= day <= w_to:
                    week_start_day = w_from
                    week_end_day = min(w_to, max_day)
                    break

            week_start = now_local.replace(
                day=week_start_day, hour=0, minute=0, second=0, microsecond=0
            )
            week_end = now_local.replace(
                day=week_end_day, hour=23, minute=59, second=59, microsecond=0
            )

            date_from_str = week_start.strftime("%Y-%m-%d")
            date_to_str = week_end.strftime("%Y-%m-%d")

            # Trailing-окно ДРР (7 полных дней до последней прошедшей субботы)
            drr_start, drr_end, drr_window_end = compute_drr_window(now_local)
            drr_date_from_str = drr_start.strftime("%Y-%m-%d")
            drr_date_to_str = drr_end.strftime("%Y-%m-%d")

            # FB-расход за плановое окно (для headroom)
            try:
                fb_week_usd = get_fb_week_spend(date_from_str, date_to_str)
            except Exception as exc:
                logger.warning("Budget Scaler: ошибка FB week spend — %s", exc)

            # Google-расход за плановое окно (для headroom)
            try:
                from datetime import date as _date
                google_week_usd = get_google_week_spend(
                    _date.fromisoformat(date_from_str),
                    _date.fromisoformat(date_to_str),
                )
            except Exception as exc:
                logger.warning("Budget Scaler: ошибка Google week spend — %s", exc)

            # FB-расход за trailing-окно ДРР (для юнитки)
            try:
                fb_drr_usd = get_fb_week_spend(drr_date_from_str, drr_date_to_str)
            except Exception as exc:
                logger.warning("Budget Scaler: ошибка FB DRR-window spend — %s", exc)
                fb_drr_usd = 0.0

            # Google-расход за trailing-окно ДРР (для юнитки)
            try:
                google_drr_usd = get_google_week_spend(
                    _date.fromisoformat(drr_date_from_str),
                    _date.fromisoformat(drr_date_to_str),
                )
            except Exception as exc:
                logger.warning("Budget Scaler: ошибка Google DRR-window spend — %s", exc)
                google_drr_usd = 0.0

            # AMO выручка trailing-окна ДРР (не планового окна)
            try:
                revenue_week_lcy = get_amo_week_revenue(drr_start, drr_end)
            except Exception as exc:
                logger.warning("Budget Scaler: ошибка AMO DRR-window revenue — %s", exc)

            # Курс USD→LCY
            try:
                from services.exchange_rate import get_usd_to_lcy
                usd_lcy_rate = get_usd_to_lcy()
            except Exception as exc:
                logger.warning("Budget Scaler: не удалось получить курс — %s", exc)
                import config as _config

                usd_lcy_rate = float(_config.USD_TO_LCY)  # fallback — настроенный курс

            # Headroom — по ПЛАНОВОМУ окну (расход капает равномерно, выручка не участвует)
            total_spend_usd = fb_week_usd + google_week_usd
            weekly_budget_usd = plan_data["budget_usd"]
            unit_target = plan_data["unit_target"]
            headroom_usd = weekly_budget_usd - total_spend_usd

            # ДРР — по TRAILING-окну до субботы: расход¤/выручка¤
            total_spend_drr_usd = fb_drr_usd + google_drr_usd
            if revenue_week_lcy > 0:
                actual_drr = (total_spend_drr_usd * usd_lcy_rate) / revenue_week_lcy
            else:
                actual_drr = None  # нет выручки — неизвестно

            # Headroom (запас недельного бюджета) — ВСЕГДА по листовому плановому
            # окну независимо от unit_source (§8 п.6: у CDP нет недельного
            # budget_usd, headroom остаётся на листе).
            if headroom_usd <= 0:
                plan_gate_reason = (
                    f"запас исчерпан: план ${weekly_budget_usd:.0f}, "
                    f"факт ${total_spend_usd:.0f} (FB ${fb_week_usd:.0f} + Google ${google_week_usd:.0f})"
                )
            elif revenue_week_lcy == 0:
                plan_gate_reason = "выручка AMO = 0 ¤ — ДРР неизвестен, не поднимаем"
            elif actual_drr is not None and actual_drr > unit_target:
                plan_gate_reason = (
                    f"юнитка превышена: факт {actual_drr*100:.1f}% > план {unit_target*100:.1f}%"
                )
        else:
            # план прочитать не удалось (read_general_plan вернул None/бросил)
            logger.warning("Budget Scaler: план не получен (sheet_id=%s) — FAIL-CLOSED, подъёмы блокируем", sheet_id)
    else:
        logger.warning("Budget Scaler: plan_sheet_id не задан — FAIL-CLOSED, подъёмы блокируем")

    # --- Каскад источника юнитки (§8, §10.4) ---
    # sheet_actual_drr/sheet_plan_gate_reason — то, что посчитал листовой путь
    # выше (может быть None/заполнено). Далее решаем, ЧЕЙ источник побеждает.
    sheet_actual_drr = actual_drr
    sheet_plan_gate_reason = plan_gate_reason

    if cdp_enabled and cdp_ue is not None:
        # CDP доступен и включён — ОН решает ДРР-гейт и план-гейт.
        # unit_target берём из листа (если доступен), иначе дефолт из спеки.
        unit_source = "cdp"
        unit_target_for_cdp = plan_data.get("unit_target") if plan_data else 0.08
        cdp_drr = cdp_ue["drr"]

        # ДРР-гейт (независим, всегда из cdp_ue.drr — Шаг A/A.2), затем темп/перерасход
        # (Шаг A.3, §6.9 — приоритет: ДРР → тормоз перерасхода → план-темп движка/кривой).
        if cdp_drr is None:
            plan_gate_reason = "CDP: выручка окна = 0 ¤ — ДРР неизвестен, не поднимаем"
        elif cdp_drr > unit_target_for_cdp:
            plan_gate_reason = (
                f"CDP: юнитка превышена: факт {cdp_drr*100:.1f}% > план {unit_target_for_cdp*100:.1f}%"
            )
        elif pace_source == "engine" and engine_pace["overspend_block"]:
            plan_gate_reason = engine_pace["overspend_reason"]  # тормоз перерасхода (§6.9)
        elif pace_source == "engine" and not engine_pace["plan_ok"]:
            plan_gate_reason = engine_pace["plan_reason"]  # темп из движка
        elif pace_source != "engine" and not cdp_ue["plan_ok"]:
            plan_gate_reason = cdp_ue["plan_reason"]  # темп из сезонной кривой (A.2)
        elif (
            # --- Этап 4: sanity-стоп-кран ---
            # Движок РАЗРЕШАЕТ (plan_ok=True), но фактическая доля месяца жёстко
            # отстаёт от сезонной нормы (< expected_share × ratio) → не поднимаем.
            # Прогноз мог завысить будущую выручку, а факт сегодня уже провален.
            pace_source == "engine"
            and engine_pace["plan_ok"]
            and sanity_cap_enabled
            and cdp_ue.get("fact_share") is not None
            and (cdp_ue.get("expected_share") or 0) > 0
            and cdp_ue["fact_share"] < cdp_ue["expected_share"] * sanity_cap_ratio
        ):
            sanity_cap_hit = True
            plan_gate_reason = (
                f"стоп-кран: прогноз разрешает, но факт {cdp_ue['fact_share']*100:.0f}% "
                f"жёстко отстаёт от сезонной нормы ≥{cdp_ue['expected_share']*100:.0f}% "
                f"(порог {sanity_cap_ratio*100:.0f}%) — не поднимаю"
            )
        else:
            plan_gate_reason = None
        # Headroom по листу (если доступен) продолжает действовать как доп. предохранитель —
        # он уже мог заблокировать выше (sheet_plan_gate_reason из headroom-ветки); если
        # лист вообще недоступен (plan_data=None), headroom-гейт просто пропускается
        # (§8 п.6 — не выдумываем headroom из CDP).
        if plan_data and headroom_usd is not None and headroom_usd <= 0 and plan_gate_reason is None:
            plan_gate_reason = sheet_plan_gate_reason
        actual_drr = cdp_drr  # для текста Telegram-заголовка используем принявший решение источник
        revenue_week_lcy = cdp_ue["revenue_lcy"]  # выручка окна для лога — тоже от источника решения
        if drr_window_end is None:
            drr_window_end = cdp_ue["drr_window_end"]  # лист недоступен — берём label из CDP
    elif plan_data is not None or sheet_plan_gate_reason:
        # CDP не решает (выключен или недоступен) — решает лист (как раньше).
        unit_source = "sheet"
        plan_gate_reason = sheet_plan_gate_reason
        pace_source = "sheet"  # движок к листовому пути не применяем (§6.7)
    else:
        # Оба источника недоступны: cdp.enabled=false/CDP None И лист недоступен.
        unit_source = "none"
        plan_gate_reason = None  # заполнится ниже фразой fail-closed
        pace_source = "none"

    # D4: FAIL-CLOSED — без валидного медиаплана НЕ поднимаем бюджеты.
    # Раньше отсутствие плана молча пропускало план-гейт (fail-open) и позволяло
    # поднимать бюджеты без проверки недельного плана/ДРР.
    if unit_source == "none" and not plan_gate_reason:
        plan_gate_reason = "медиаплан недоступен (ни CDP, ни лист) — подъёмы заблокированы (fail-closed)"

    # --- Shadow-сверка (§10.4): считаем и логируем ВСЕГДА, независимо от источника ---
    drr_cdp = cdp_ue["drr"] if cdp_ue else None
    drr_divergence_pp = (
        abs(drr_cdp - sheet_actual_drr) * 100.0
        if (drr_cdp is not None and sheet_actual_drr is not None)
        else None
    )
    logger.info(
        "Budget Scaler CDP-shadow: source=%s drr_cdp=%s drr_sheet=%s divergence=%s п.п. pace_source=%s",
        unit_source, drr_cdp, sheet_actual_drr, drr_divergence_pp, pace_source,
    )

    # --- Телеметрия сезонного план-гейта (Шаг A.2, §6.3/§10.1): plan_gate_mode
    # "seasonal" только когда решение реально принимал CDP-путь, иначе "none".
    plan_gate_mode = cdp_ue.get("plan_gate_mode", "none") if (cdp_enabled and cdp_ue is not None) else "none"
    expected_share = cdp_ue.get("expected_share") if cdp_ue else None
    fact_share = cdp_ue.get("fact_share") if cdp_ue else None

    # --- Неполнота Google в окне ДРР ---
    # Окно ДРР молча суммирует отсутствующие дни Google как 0 (подрядчик грузит
    # вкладки с задержкой). Считаем, сколько дней окна БЕЗ снимка, чтобы честно
    # пометить это в подписи ДРР и телеметрии. Окно = drr_window_end − 6 дней ..
    # drr_window_end (та же арифметика, что и в _fmt_drr_signature).
    google_missing_days = 0
    if drr_window_end is not None:
        try:
            from services.google_spend import count_missing_google_days
            _g_from = (drr_window_end - timedelta(days=6)).date().isoformat()
            _g_to = drr_window_end.date().isoformat()
            google_missing_days = count_missing_google_days(_g_from, _g_to)
        except Exception as exc:
            logger.warning("Budget Scaler: не удалось посчитать неполноту Google — %s", exc)

    # Единый словарь телеметрии для распаковки во ВСЕ поздние return (§10.1 п.4) —
    # чтобы не дублировать 11 полей вручную в каждой ветке.
    _telem = {
        "unit_source": unit_source,
        "drr_cdp": drr_cdp,
        "drr_sheet": sheet_actual_drr,
        "drr_divergence_pp": drr_divergence_pp,
        "plan_gate_mode": plan_gate_mode,
        "expected_share": expected_share,
        "fact_share": fact_share,
        # --- Шаг A.3: план-гейт v3 (прогнозный движок budget-context) ---
        "pace_source": pace_source,
        "forecast_eom": engine_pace["forecast_eom"] if engine_pace else None,
        "pace_vs_expected": engine_pace["pace_vs_expected"] if engine_pace else None,
        "engine_wape": engine_pace["engine_wape"] if engine_pace else None,
        # Неполнота Google в окне ДРР
        "google_missing_days": google_missing_days,
        # --- Этап 4: самопроверка прогноза + sanity-стоп-кран ---
        "selfcheck_dev": selfcheck_dev,   # отклонение факта от ожидания (<0 = отстаём)
        "sanity_cap_hit": sanity_cap_hit,  # сработал ли стоп-кран сезонной нормы
        # --- Wave 3C: статус fail-closed гейта самопроверки ---
        "selfcheck_status": selfcheck_status,
        # --- Волна 3 когорт: тренд. Счётчики наполняются ниже, в цикле отбора
        # адсетов-кандидатов; поздние return распаковывают уже обновлённый dict.
        "trend_mode": trend_ctx.mode.value,
        "trend_vetoed": 0,
        "trend_shadow_vetoed": 0,
    }

    # --- Протокол сомнений (§6.4, §8.4-8.6 спеки, не блокирующий) ---
    # «Поднимаю» + тишина о сомнениях = дезинформация, поэтому
    # сомнения БОЛЬШЕ НЕ уходят отдельным сообщением ДО выбора победителей и
    # исполнения. Здесь только СЧИТАЕМ триггеры и пишем журнал; сами тексты
    # приклеиваются к ОДНОМУ финальному сообщению о РЕЗУЛЬТАТЕ (doubt_footer ниже).
    doubts: list[str] = []  # всегда определён (используется во всех финальных сообщениях)
    if cdp_cfg.get("doubt_alerts", True):
        fallback_happened = cdp_enabled and (cdp_ue is None)
        unit_target_for_doubt = (plan_data.get("unit_target") if plan_data else 0.08)
        try:
            doubts = _evaluate_doubt_triggers(
                unit_source=unit_source,
                cdp_ue=cdp_ue,
                drr_cdp=drr_cdp,
                drr_sheet=sheet_actual_drr,
                drr_divergence_pp=drr_divergence_pp,
                unit_target=unit_target_for_doubt,
                plan_gate_reason=plan_gate_reason,
                fallback_happened=fallback_happened,
                engine_distrust_reason=engine_distrust_reason,
                engine_revenue_behind_spend_ok=engine_revenue_behind_spend_ok,
                thresholds=cfg,  # пороги doubt_* лежат в SCALE_DEFAULTS (влиты в cfg)
            )
        except Exception as exc:
            logger.warning("Budget Scaler: протокол сомнений упал неожиданно — %s", exc)
            doubts = []
        if doubts:
            # decision_line — решение ПО ГЕЙТАМ (известно сейчас, до исполнения);
            # пишем в журнал сомнений (data/doubt_log.json) для секции «Итог дня».
            decision_line = (
                "поднимаю бюджет (гейты прошли)" if plan_gate_reason is None
                else f"НЕ поднимаю: {plan_gate_reason}"
            )
            logger.info("Budget Scaler: протокол сомнений — %d триггер(ов): %s", len(doubts), doubts)
            # Журнал сомнений — не блокирующий (ошибка записи не роняет прогон).
            try:
                from services.doubt_log import append_doubt_entry
                append_doubt_entry(doubts, decision_line)
            except Exception as exc:
                logger.warning("Budget Scaler: не удалось записать журнал сомнений — %s", exc)

    # --- Единые части финальных сообщений ---
    # Подпись ДРР (значение + «чей» + окно + цель) и хвост «сомнения + Делаю сам»
    # считаем ОДИН раз здесь и используем во ВСЕХ терминальных ветках ниже.
    unit_target_effective = (
        float(plan_data["unit_target"])
        if (plan_data and plan_data.get("unit_target") is not None)
        else 0.08
    )
    drr_sig = _fmt_drr_signature(actual_drr, drr_window_end, unit_target_effective, google_missing_days)
    doubt_footer = _fmt_doubts_and_footer(doubts)

    if plan_gate_reason:
        # Вариант 3: гейт заблокировал — «НЕ поднимаю: причина»
        # + контекст цифрами (план/факт/запас) + подпись ДРР + сомнения в конце.
        week_label = plan_data.get("week_label", "неизвестно") if plan_data else "неизвестно"
        weekly_budget_usd_val = plan_data.get("budget_usd", 0.0) if plan_data else 0.0
        headroom_str = fmt_money(headroom_usd, "$") if headroom_usd is not None else "n/a"
        total_fact = fb_week_usd + google_week_usd
        msg = (
            f"📊 <b>Budget Scaler ({html.escape(week_label)})</b>\n"
            f"❌ НЕ поднимаю: {plan_gate_reason}\n"
            f"План {fmt_money(weekly_budget_usd_val, '$')} · "
            f"Факт FB {fmt_money(fb_week_usd, '$')} + Google {fmt_money(google_week_usd, '$')} "
            f"= {fmt_money(total_fact, '$')} · Запас {headroom_str}\n"
            f"{drr_sig}\n"
            f"Выручка (окно ДРР) {fmt_money(revenue_week_lcy, '¤')}"
            + doubt_footer
        )
        send_telegram(msg)
        logger.info("Budget Scaler: план-гейт заблокировал повышение — %s", plan_gate_reason)
        return {
            "ran": True,
            "skipped_reason": f"plan_gate: {plan_gate_reason}",
            "mode": mode,
            "winners": [],
            "recommendations": [],
            "scaled": [],
            "errors": [],
            **_telem,
        }

    # --- Шаг 2: получаем активные объявления из локальной БД ---
    try:
        from services.shadow_report import _fetch_ads_from_local_db
        local_ads = _fetch_ads_from_local_db()
    except Exception as exc:
        logger.error("Budget Scaler: _fetch_ads_from_local_db упал: %s", exc)
        send_critical_alert("Budget Scaler: ошибка сбора данных из БД", str(exc))
        return {
            "ran": False, "skipped_reason": f"db_error: {exc}",
            "mode": mode, "winners": [], "recommendations": [], "scaled": [],
            "errors": [f"db_error: {exc}"],
            **_telem,
        }

    if not local_ads:
        logger.info("Budget Scaler: нет активных объявлений в локальной БД")
        send_telegram(_msg_no_raise("нет активных объявлений", drr_sig, doubt_footer))
        return {
            "ran": True, "skipped_reason": None,
            "mode": mode, "winners": [], "recommendations": [], "scaled": [], "errors": [],
            **_telem,
        }

    # --- Источник-каскад оплат (§6.5 ARCH-cdp-payments): переключатель ---
    # amo/shadow (дефолт) — старое поведение 1:1; erp — payments_effective (max).
    _payments_source = cdp_cfg.get("payments_source", "shadow")
    _use_erp_payments = _payments_source == "erp"

    # --- Честный 7d-гейт (Wave 3A, opt-in): подтверждено ли source-specific
    # окно ровно за 7 дней? При require_fresh_7d=True и mode="active" без
    # подтверждённого свежего полного 7d-окна active-подъём блокируется
    # (fail-closed). Дефолт False — поведение по умолчанию не меняется. Само
    # окно/полнота считаются source-specific refresh'ами (amo_outcomes.
    # refresh_amo_payments_7d / cdp_payments.refresh_payments_erp_7d).
    _require_7d = _require_fresh_7d(cfg_v2)
    _win7d_confirmed = False
    if _require_7d:
        from services.cdp_payments import seven_d_window_confirmed
        _win7d_confirmed = seven_d_window_confirmed(
            local_ads, _payments_source, now=datetime.now(_TZ_LOCAL)
        )
        logger.info(
            "Budget Scaler honest-7d: require_fresh_7d=True source=%s confirmed=%s",
            _payments_source, _win7d_confirmed,
        )

    # Shadow-сверка оплат AMO vs ERP (§10.4): считаем и логируем ВСЕГДА (один раз
    # за прогон, анти-спам); на решение влияет только при payments_source="erp".
    amo_payments_sum = sum(int(a.get("payments") or 0) for a in local_ads if a.get("payments") is not None)
    c1_payments_sum = sum(int(a.get("payments_erp") or 0) for a in local_ads if a.get("payments_erp") is not None)
    logger.info(
        "Budget Scaler payments-shadow: source=%s amo=%d erp=%d divergence=%d",
        _payments_source, amo_payments_sum, c1_payments_sum, abs(amo_payments_sum - c1_payments_sum),
    )

    # --- Шаг 3: отбор кандидатов по ФАКТУ ПРОДАЖ (не score_and_decide) ---
    # _select_sales_candidates фильтрует объявления с payments>0 и сортирует
    # по силе сигнала: payments↓, qual_pct↓, romi↓ (§10.2 спеки Фазы 2).
    local_by_id: dict[str, dict] = {ad["ad_id"]: ad for ad in local_ads}
    scale_winners = _select_sales_candidates(local_ads, use_erp_payments=_use_erp_payments)

    if not scale_winners:
        logger.info("Budget Scaler: кандидатов по продажам нет (payments>0) — масштабировать нечего")
        send_telegram(_msg_no_raise(
            f"победителей по оплатам не нашлось (нет объявлений с оплатами за {_sales_window_label()})",
            drr_sig, doubt_footer,
        ))
        return {
            "ran": True, "skipped_reason": None,
            "mode": mode, "winners": [], "recommendations": [], "scaled": [], "errors": [],
            **_telem,
        }

    # --- Шаг 4: получаем adset_id ВСЕХ активных объявлений из FB ---
    # (в creative_kb adset_id отсутствует — нужен живой запрос)
    # ВАЖНО: запрашиваем по ВСЕМ local_ads, а не только по winner_ad_ids (кандидатам
    # с payments>0). Иначе объявления-сливы (payments=0) не попадут в fb_ad_info и,
    # соответственно, в ads_by_adset — групповые защиты (confirmed_waster/learning,
    # см. §10.2) не увидят их внутри адсета, и адсет со сливом получит подъём
    # («долив в слив» — запрещено критерием №6 спеки).
    all_ad_ids = [ad["ad_id"] for ad in local_ads]
    try:
        from services.autopilot import _fetch_candidate_fb_info
        fb_ad_info = _fetch_candidate_fb_info(all_ad_ids)
    except Exception as exc:
        logger.error("Budget Scaler: _fetch_candidate_fb_info упал: %s", exc)
        send_critical_alert("Budget Scaler: ошибка получения adset_id из FB", str(exc))
        return {
            "ran": False, "skipped_reason": f"fb_ad_info_error: {exc}",
            "mode": mode, "winners": [d["ad_id"] for d in scale_winners],
            "recommendations": [], "scaled": [],
            "errors": [f"fb_ad_info_error: {exc}"],
            **_telem,
        }

    # Группируем ВСЕ объявления local_ads по adset_id (не только кандидатов) —
    # нужно для групповых защит (waster/learning), которые смотрят на ВСЕ
    # объявления адсета, а не только на объявление-кандидат.
    # fb_ad_info запрошен по all_ad_ids (шаг 4 выше), поэтому adset_id известен
    # для всех активных объявлений, включая сливы и learning-объявления.
    ads_by_adset: dict[str, list[dict]] = {}
    for ad in local_ads:
        ad_id = ad["ad_id"]
        fb_info = fb_ad_info.get(ad_id)
        # fb_info может отсутствовать, если FB не вернул данные для этого id
        # (например, объявление уже удалено/архивировано между сбором local_ads
        # и запросом к FB) — пропускаем такое объявление из групповых защит.
        if fb_info is None:
            continue
        adset_id = fb_info.get("adset_id", "")
        if not adset_id:
            continue
        ads_by_adset.setdefault(adset_id, []).append(ad)

    # Собираем уникальные adset_id по кандидатам-победителям (сохраняя порядок
    # сортировки _select_sales_candidates — сначала сильнейший сигнал продаж).
    adset_candidates: list[dict] = []
    seen_adsets: set[str] = set()
    # Волна 3 когорт: что тренд сделал (или сделал бы) с подъёмами — для отчёта.
    trend_vetoed: list[str] = []         # реально не подняли из-за тренда
    trend_shadow_vetoed: list[str] = []  # не подняли бы, будь режим active
    for d in scale_winners:
        ad_id = d["ad_id"]
        fb_info = fb_ad_info.get(ad_id, {})
        adset_id = fb_info.get("adset_id", "")
        if not adset_id:
            logger.warning(
                "Budget Scaler: нет adset_id для %s (%s) — пропускаем",
                ad_id, d.get("ad_name", ""),
            )
            continue
        if adset_id in seen_adsets:
            # Несколько кандидатов в одном адсете — берём только первого
            # (сильнейший сигнал продаж, т.к. scale_winners уже отсортирован)
            continue

        # Групповые защиты (§10.2): смотрим на ВСЕ объявления этого адсета
        adset_ads = ads_by_adset.get(adset_id, [d])

        veto, veto_reason = _adset_waster_veto(adset_ads, _use_erp_payments, cfg_v2)
        if veto:
            logger.info(
                "Budget Scaler: адсет %s исключён — %s", adset_id, veto_reason,
            )
            continue

        # ВЕТО ТРЕНДА (волна 3 недельных когорт). Тем же паттерном, что вето
        # слива, и с той же асимметрией: тренд может ТОЛЬКО запретить подъём.
        # Поднять он не может никогда — ни РОСТ, ни любой другой вердикт здесь
        # не рассматривается (см. services/trend_gate.py). МАЛО ДАННЫХ / ПЛАТО /
        # РОСТ, уровень объявления, сбой чтения когорт — вето НЕ дают.
        trend_res = trend_gate.evaluate_raise_veto(trend_ctx, adset_id)
        if trend_res.applied:
            trend_vetoed.append(
                f"{d.get('ad_name') or ad_id} (adset {adset_id}): {trend_res.reason}"
            )
            logger.info(
                "Budget Scaler: адсет %s исключён — %s", adset_id, trend_res.reason,
            )
            continue
        if trend_res.matched:
            # shadow: исход НЕ меняем, но в отчёте видно, что тренд сказал бы.
            trend_shadow_vetoed.append(
                f"{d.get('ad_name') or ad_id} (adset {adset_id}): {trend_res.reason}"
            )
            logger.info(
                "Budget Scaler: тренд ветировал бы адсет %s — %s",
                adset_id, trend_res.reason,
            )

        if not any(int(a.get("days_running") or 0) >= _MIN_DAYS_RUNNING_FOR_SCALE for a in adset_ads):
            logger.info(
                "Budget Scaler: адсет %s исключён — все объявления в learning-фазе (days_running<%d)",
                adset_id, _MIN_DAYS_RUNNING_FOR_SCALE,
            )
            continue

        seen_adsets.add(adset_id)

        # Агрегация: payments = сумма по объявлениям адсета с оплатами;
        # qual_pct/romi = максимум (лучший сигнал адсета).
        total_payments = sum(
            int(_eff_payments(a, _use_erp_payments) or 0)
            for a in adset_ads
            if _has_payment(a, use_erp_payments=_use_erp_payments)
        )
        qual_values = [float(a["qual_pct"]) for a in adset_ads if a.get("qual_pct") is not None]
        romi_values = [float(a["romi"]) for a in adset_ads if a.get("romi") is not None]

        local_ad = local_by_id.get(ad_id, {})
        adset_candidates.append({
            "ad_id": ad_id,
            "ad_name": d.get("ad_name", ""),
            "adset_id": adset_id,
            "qual_pct": max(qual_values) if qual_values else local_ad.get("qual_pct"),
            "romi": max(romi_values) if romi_values else local_ad.get("romi"),
            "spend": local_ad.get("spend", 0),
            "leads": local_ad.get("leads", 0),
            "payments": total_payments,
        })

    # Наблюдаемость тренда: счётчики видны в любом позднем return (_telem
    # распаковывается уже обновлённым) и в тексте отчёта ниже.
    _telem["trend_vetoed"] = len(trend_vetoed)
    _telem["trend_shadow_vetoed"] = len(trend_shadow_vetoed)
    trend_note = _fmt_trend_note(trend_vetoed, trend_shadow_vetoed)

    # Ограничиваем cap ПОСЛЕ групповых защит — берём сильнейшие адсеты
    adset_candidates = adset_candidates[:cap]
    scale_winners_kept_ids = {c["ad_id"] for c in adset_candidates}
    scale_winners = [d for d in scale_winners if d["ad_id"] in scale_winners_kept_ids] or scale_winners[:cap]

    if not adset_candidates:
        logger.info("Budget Scaler: нет адсетов-кандидатов после групповых защит — пропускаем")
        send_telegram(_msg_no_raise(
            "все адсеты-победители отсеяны (слив, падающий тренд или новый адсет младше 3 дн)",
            drr_sig, doubt_footer + trend_note,
        ))
        return {
            "ran": True, "skipped_reason": "no_adset_candidates",
            "mode": mode,
            "winners": [d["ad_id"] for d in scale_winners],
            "recommendations": [], "scaled": [], "errors": [],
            **_telem,
        }

    # --- Шаг 5: получаем бюджеты из FB ---
    # 5a. ВСЕ активные адсеты аккаунта — для точного подсчёта суммарного бюджета.
    #     Нельзя ограничиваться только кандидатами: если аккаунт тратит $3900 на
    #     других адсетах, проверка только кандидатов ложно пропустит повышение.
    try:
        all_account_adsets = _fetch_all_account_adset_budgets()
    except Exception as exc:
        logger.error("Budget Scaler: _fetch_all_account_adset_budgets упал: %s", exc)
        send_critical_alert("Budget Scaler: ошибка получения всех бюджетов из FB", str(exc))
        return {
            "ran": False, "skipped_reason": f"fb_all_budgets_error: {exc}",
            "mode": mode,
            "winners": [d["ad_id"] for d in scale_winners],
            "recommendations": [], "scaled": [],
            "errors": [f"fb_all_budgets_error: {exc}"],
            **_telem,
        }

    # Fail-closed: FB отдал не-200 в ходе пагинации → суммарный бюджет неполон.
    # НЕ поднимаем бюджеты в этот прогон (иначе можно ложно пробить общий
    # потолок max_total_daily_budget), но остальная логика прогона не страдает —
    # честно сообщаем в отчёт и выходим.
    if all_account_adsets is None:
        logger.warning(
            "Budget Scaler: подъём пропущен — не удалось посчитать текущий суммарный бюджет (FB non-200)"
        )
        send_telegram(_msg_no_raise(
            "подъём пропущен: не удалось посчитать текущий суммарный бюджет",
            drr_sig, doubt_footer,
        ))
        return {
            "ran": True, "skipped_reason": "total_budget_uncounted",
            "mode": mode,
            "winners": [d["ad_id"] for d in scale_winners],
            "recommendations": [], "scaled": [],
            "errors": ["total_budget_uncounted"],
            **_telem,
        }

    # 5b. Бюджеты конкретных кандидатов — нужны для текущего бюджета каждого адсета.
    #     Сначала пробуем из общего ответа (уже есть), для отсутствующих — запрос по id.
    candidate_adset_ids = [c["adset_id"] for c in adset_candidates]
    missing_ids = [aid for aid in candidate_adset_ids if aid not in all_account_adsets]
    candidate_extra: dict = {}
    if missing_ids:
        # Адсет кандидата не попал в список активных (возможно, статус отличается от ACTIVE).
        # Запрашиваем отдельно чтобы узнать его текущий статус и бюджет.
        try:
            candidate_extra = _fetch_adset_budgets(missing_ids)
        except Exception as exc:
            logger.warning("Budget Scaler: _fetch_adset_budgets для missing упал: %s", exc)
    # Объединяем: данные всего аккаунта + дополнение по кандидатам
    adset_budgets = {**all_account_adsets, **candidate_extra}

    # --- Шаг 6: базовый суммарный бюджет = сумма по ВСЕМ активным адсетам аккаунта ---
    # Это реальная картина расходов; накопительно будем добавлять каждое повышение.
    current_total_usd = sum(
        info["daily_budget_usd"] for info in all_account_adsets.values()
    )
    logger.info(
        "Budget Scaler: суммарный бюджет аккаунта $%.2f (%d адсетов, потолок $%.2f)",
        current_total_usd, len(all_account_adsets), max_total_usd,
    )

    # --- Шаг 7: формируем план повышений ---
    recommendations: list[dict] = []
    errors: list[str] = []
    # now_local гарантированно определяем здесь (может быть уже вычислена выше
    # при чтении плана, но не гарантированно в scope при всех ветках) — для
    # дневного капа (budget_daily_cap) нужен единый момент времени на весь прогон.
    now_local = datetime.now(_TZ_LOCAL)

    # --- Wave 1B.2: сверка зависших pending-резерваций перед планированием ---
    # Прошлый active-прогон мог упасть между FB-мутацией и локальным commit,
    # оставив pending. Сверяем по фактическому бюджету из FB (already fetched):
    # применён → commit, не менялся → release, неясно/недоступен → оставить pending
    # (fail-closed, лимит остаётся занятым). Только в active — dry_run не резолвит.
    if mode == "active":
        try:
            for pend_aid in budget_daily_cap.list_pending_adsets(now=now_local):
                info = adset_budgets.get(pend_aid)
                actual_budget = info.get("daily_budget_usd") if isinstance(info, dict) else None
                rec_res = budget_daily_cap.reconcile_pending(pend_aid, actual_budget, now=now_local)
                if rec_res.get("committed") or rec_res.get("released") or rec_res.get("kept"):
                    logger.info(
                        "Budget Scaler: сверка pending адсета %s — %s", pend_aid, rec_res,
                    )
        except Exception as exc:
            logger.warning("Budget Scaler: сверка pending-резерваций упала: %s", exc)

    for candidate in adset_candidates:
        adset_id = candidate["adset_id"]
        ad_name = candidate["ad_name"]
        adset_info = adset_budgets.get(adset_id)

        if adset_info is None:
            # FB не вернул данные по этому адсету
            msg = f"нет данных бюджета для adset {adset_id} (объявление {ad_name})"
            logger.warning("Budget Scaler: %s", msg)
            errors.append(msg)
            continue

        # Трогаем ТОЛЬКО ACTIVE — любой другой статус (PAUSED, CAMPAIGN_PAUSED,
        # ADSET_PAUSED, ARCHIVED и т.д.) пропускаем. Нельзя поднимать бюджет
        # адсета, чья кампания или сам адсет на паузе.
        adset_status = adset_info.get("effective_status", "UNKNOWN")
        if adset_status != "ACTIVE":
            logger.info(
                "Budget Scaler: адсет %s (%s) статус %s ≠ ACTIVE — пропускаем",
                adset_id, adset_info.get("name", ""), adset_status,
            )
            continue

        current_budget = adset_info["daily_budget_usd"]

        # --- Дневной кап (§10.1 спеки Фазы 2) ---
        # daily_cap_pct = max_increase_pct (теперь = 15 по дефолту, семантика "в сутки на адсет").
        daily_cap_pct = max_increase_pct
        remaining_pct = budget_daily_cap.get_remaining_daily_pct(
            adset_id, current_budget, daily_cap_pct, now=now_local,
        )
        if remaining_pct <= 0:
            logger.info(
                "Budget Scaler: адсет %s — дневной лимит %.0f%% исчерпан, пропускаем",
                adset_id, daily_cap_pct,
            )
            continue

        effective_pct = min(daily_cap_pct, remaining_pct)
        start_budget = budget_daily_cap.get_day_start_budget(adset_id, current_budget, now=now_local)

        new_budget = _calc_new_budget(
            current_budget_usd=current_budget,
            start_budget_usd=start_budget,
            effective_pct=effective_pct,
            max_mult=max_mult,
            max_abs_usd=max_abs_usd,
        )

        # Бюджет уже на потолке — нет смысла изменять
        if new_budget <= current_budget:
            logger.info(
                "Budget Scaler: адсет %s уже на потолке ($%.2f) — пропускаем",
                adset_id, current_budget,
            )
            continue

        # Проверяем общий потолок: повышение не должно пробить max_total_daily_budget
        delta = new_budget - current_budget
        if current_total_usd + delta > max_total_usd:
            logger.warning(
                "Budget Scaler: повышение адсета %s ($%.2f→$%.2f) пробьёт общий потолок "
                "$%.2f (текущий $%.2f) — пропускаем",
                adset_id, current_budget, new_budget, max_total_usd, current_total_usd,
            )
            errors.append(
                f"адсет {adset_id}: повышение пробивает общий потолок ${max_total_usd:.0f} — пропущено"
            )
            continue

        applied_pct = (new_budget - current_budget) / start_budget * 100.0 if start_budget > 0 else 0.0
        # raised_pct ДО этого подъёма (ещё не записан record_raise) + applied_pct этого подъёма
        day_raised_before = daily_cap_pct - remaining_pct
        day_raised_pct_after = day_raised_before + applied_pct

        recommendations.append({
            "adset_id": adset_id,
            "adset_name": adset_info.get("name", ""),
            "ad_id": candidate["ad_id"],
            "ad_name": ad_name,
            "current_budget_usd": current_budget,
            "new_budget_usd": new_budget,
            "delta_usd": delta,
            "qual_pct": candidate.get("qual_pct"),
            "romi": candidate.get("romi"),
            "leads": candidate.get("leads"),
            "payments": candidate.get("payments", 0),
            "quals": candidate.get("quals", candidate.get("qualified", 0)),
            "spend": candidate.get("spend", 0),
            "revenue_lcy": candidate.get("revenue_lcy", candidate.get("revenue", 0)),
            "applied_pct": applied_pct,
            "day_raised_pct_after": day_raised_pct_after,
        })
        # Учитываем потенциальное повышение в текущем total (для следующей итерации)
        current_total_usd += delta

    # --- Шаг 8: dry_run или active ---
    scaled: list[dict] = []
    proposal_ids: list[str] = []

    if not recommendations:
        send_telegram(_msg_no_raise(
            "победители упёрлись в потолки бюджета",
            drr_sig, doubt_footer,
        ))
        return {
            "ran": True, "skipped_reason": None,
            "mode": mode,
            "winners": [d["ad_id"] for d in scale_winners],
            "recommendations": [],
            "scaled": [],
            "errors": errors,
            **_telem,
        }

    if mode == "dry_run":
        # Только рекомендации — без изменений бюджета
        lines = []
        total_delta = 0.0
        for rec in recommendations:
            delta = rec["new_budget_usd"] - rec["current_budget_usd"]
            total_delta += delta
            adset_name = truncate_at_word_boundary(rec.get("adset_name", ""), 30)
            lines.append(
                f"• {html.escape(adset_name)}: {fmt_money(rec['current_budget_usd'], '$')} → "
                f"{fmt_money(rec['new_budget_usd'], '$')} в день (+{rec['applied_pct']:.0f}%)"
            )
            # --- Запись в decisions: DRY_RUN_RAISE (БЕЗ ИЗМЕНЕНИЙ, §10.3 спеки) ---
            # Ошибка записи НЕ прерывает остальные — только логируем и добавляем в errors.
            try:
                from agent.repositories import decisions_repo
                decisions_repo.save_decision(
                    "default", rec["ad_id"], rec["ad_name"],
                    "DRY_RUN_RAISE",
                    reason=f"поднял бы {rec['adset_name']}: ${rec['current_budget_usd']:.0f}→"
                           f"${rec['new_budget_usd']:.0f} (+{rec['applied_pct']:.0f}%), "
                           f"{rec['payments']} оплат за {_sales_window_label()}",
                    confirmed_by="budget_pilot_dry",
                    spend=None, leads=rec.get("leads"),
                    romi=rec.get("romi"), qual_pct=rec.get("qual_pct"),
                )
            except Exception as exc:
                msg = f"decisions DRY_RUN_RAISE для {rec['ad_id']}: {exc}"
                logger.error("Budget Scaler: %s", msg)
                errors.append(msg)
        text = (
            f"🧪 <b>Budget Scaler (репетиция): поднял бы {len(recommendations)} "
            f"{_pluralize_adsets(len(recommendations))}</b>\n"
            + "\n".join(lines)
            + f"\nИтого добавил бы: {fmt_money(total_delta, '$')}/день\n"
            + f"{drr_sig}"
            + doubt_footer
            + trend_note
        )
        send_telegram(text)
        logger.info("Budget Scaler dry_run: %d рекомендаций", len(recommendations))

    elif mode == "active":
        # --- Wave 3C: fail-closed гейт самопроверки движка ---
        # Когда гейт активен (CDP + engine_selfcheck_enabled), active-подъём разрешён
        # ТОЛЬКО при валидной самопроверке (status="ok"). Любой сбой/прогрев/недоверие
        # → подъём заблокирован (FB не трогаем). Предохранитель ПОВЕРХ инвариантов
        # Фазы 2 и Wave 3A (вето слива и честное 7d-окно остаются строгими).
        if selfcheck_gate_active and not selfcheck_gate_ok:
            block_reason = selfcheck_skip_reason or "самопроверка движка не пройдена"
            logger.info(
                "Budget Scaler: active-подъём заблокирован — самопроверка fail-closed "
                "(status=%s): %s", selfcheck_status, block_reason,
            )
            send_telegram(_msg_no_raise(
                f"{block_reason} — подъём заблокирован (fail-closed)",
                drr_sig, doubt_footer,
            ))
            return {
                "ran": True,
                "skipped_reason": f"selfcheck_{selfcheck_status}",
                "mode": mode,
                "winners": [d["ad_id"] for d in scale_winners],
                "recommendations": [], "scaled": [], "errors": errors,
                **_telem,
            }

        # --- Честный 7d-гейт (Wave 3A, fail-closed): при require_fresh_7d=True
        # active-подъём разрешён ТОЛЬКО с подтверждённым свежим полным 7d-окном.
        # Stale/partial/unknown → никакого active raise (FB не трогаем). Это
        # предохранитель ПОВЕРХ инвариантов Фазы 2 (вето слива остаётся строгим).
        if _require_7d and not _win7d_confirmed:
            logger.info(
                "Budget Scaler: active-подъём заблокирован — честное 7d-окно не "
                "подтверждено (require_fresh_7d, source=%s)", _payments_source,
            )
            send_telegram(_msg_no_raise(
                "честное 7-дневное окно оплат не подтверждено (нет свежих полных "
                "данных источника) — подъём заблокирован (fail-closed)",
                drr_sig, doubt_footer,
            ))
            return {
                "ran": True, "skipped_reason": "stale_7d_window",
                "mode": mode,
                "winners": [d["ad_id"] for d in scale_winners],
                "recommendations": [], "scaled": [], "errors": errors,
                **_telem,
            }

        # Active-режим теперь только создаёт owner proposals.
        from services.action_producer_gateway import propose_scale
        from services.owner_proposal_card import DecisionContext
        already_proposed: list[str] = []
        for rec in recommendations:
            adset_id = rec["adset_id"]
            new_budget = rec["new_budget_usd"]
            try:
                # Scope обязан однозначно определять intent: ad-победитель тоже
                # входит в payload предложения, поэтому без него один scope мог
                # достаться двум разным payload — это конфликт ключа, а не
                # идемпотентный повтор.
                outcome = propose_scale(
                    rec,
                    scope=(
                        f"budget-scaler:{now_local.date().isoformat()}:{adset_id}:"
                        f"{rec['ad_id']}:{rec['current_budget_usd']}:{new_budget}"
                    ),
                    # Экономика победителя в самой карточке: без неё владелец
                    # видел «Изменить бюджет adset <id>» без единой цифры «за что».
                    decision=DecisionContext(
                        spend_usd=rec.get("spend"),
                        leads=rec.get("leads"),
                        qual_leads=rec.get("quals"),
                        qual_pct=rec.get("qual_pct"),
                        payments=rec.get("payments"),
                        romi_pct=rec.get("romi"),
                        business_reason=(
                            f"победитель «{rec.get('ad_name') or rec['ad_id']}» "
                            f"даёт ROMI {rec.get('romi')}% — поднимаем на "
                            f"{rec.get('applied_pct')}%"
                            if rec.get("romi") is not None
                            else None
                        ),
                    ),
                    now=now_local,
                )
                if outcome.receipt is not None:
                    rec["proposal_id"] = outcome.receipt.proposal_id
                    if outcome.receipt.deduplicated:
                        # Тот же intent уже висит у владельца: второго
                        # предложения не создаём и в отчёт не шумим.
                        already_proposed.append(outcome.receipt.proposal_id)
                        logger.info(
                            "Budget Scaler: proposal %s для adset %s уже ждёт "
                            "одобрения — новый не создаём",
                            outcome.receipt.proposal_id,
                            adset_id,
                        )
                        continue
                    proposal_ids.append(outcome.receipt.proposal_id)
                    logger.info(
                        "Budget Scaler: proposal %s создан для adset %s",
                        outcome.receipt.proposal_id,
                        adset_id,
                    )
                else:
                    status = outcome.action
                    msg = f"SCALE proposal {adset_id} не создан: {status}"
                    logger.warning("Budget Scaler: %s", msg)
                    errors.append(msg)
                    break
            except Exception as exc:
                msg = f"adset {adset_id}: {exc}"
                logger.error("Budget Scaler: %s", msg)
                errors.append(msg)
                break

        if proposal_ids:
            # ГЕЙТ 4 (кулдаун) взводим по факту ОТПРАВЛЕННОГО предложения, а не
            # по факту подъёма: подъём выполнит execution boundary после
            # одобрения, но просить владельца об одном и том же каждые полчаса
            # нельзя. Без этой записи last_scaled_at оставался пустым и кулдаун
            # был мёртвым — крон плодил предложения на каждом тике.
            try:
                _record_scaled_at()
            except Exception as exc:
                logger.warning(
                    "Budget Scaler: не удалось записать last_scaled_at: %s", exc
                )
            send_telegram(
                "📨 <b>Budget Scaler: предложения отправлены владельцу</b>\n"
                f"Количество: {len(proposal_ids)}\n{drr_sig}{doubt_footer}{trend_note}"
            )
        elif already_proposed:
            # Новых предложений нет, старые ещё ждут — молчим, чтобы не дублировать
            # уже отправленное владельцу сообщение.
            logger.info(
                "Budget Scaler: %d предложений уже ждут одобрения — новых не создаём",
                len(already_proposed),
            )
        else:
            send_telegram(
                _msg_no_raise(
                    "предложения повышения не созданы (см. ошибки)",
                    drr_sig,
                    doubt_footer,
                )
            )

    return {
        "ran": True,
        "skipped_reason": None,
        "mode": mode,
        "winners": [d["ad_id"] for d in scale_winners],
        "recommendations": recommendations,
        "scaled": scaled,
        "proposals": proposal_ids,
        "errors": errors,
        **_telem,
    }
