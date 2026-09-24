"""
services/shadow_report.py — сбор активных объявлений из локальной БД.

История: раньше здесь жил весь «Теневой режим автопилота» (ежедневная сводка
«что бы я сделал» + рассылка в Telegram). Рассылку УБРАЛИ — она была
реликтом отменённого «режима тени», дублировала
и противоречила боевому контуру run_autopilot_live (крон _cron_shadow_report
и ручной endpoint /api/autopilot/shadow-now сняты из web/app.py).

_fetch_ads_from_local_db() — ОСТАВЛЕНА как есть: это переиспользуемая выборка
активных объявлений (creative_kb + ранние лиды из ad_daily_metrics), её
использует services.decision_policy через autopilot/budget_scaler/guardian/
coverage_monitor/brief_generator. НЕ трогать без проверки всех потребителей.

Волна 2 недельных когорт добавила сюда ТЕНЕВУЮ секцию тренда
(build_trend_shadow / format_trend_shadow): что сказало бы правило
services/trend_policy.py по каждому адсету — с указанием сравниваемых недель и
объёмов. Новый контур ради этого не заводился намеренно: сюда он ложится тем
же способом, что и остальная выборка из локальной БД. Секция строго теневая —
ни предложений, ни мутаций, ни записи решений, ни отправки в Telegram здесь
нет и быть не должно (рассылка «🔮 Тень» снята).

Живые FB/AMO-вызовы ОТСУТСТВУЮТ. Время выборки: < 1 секунды (2 SQL по индексу).
"""

import html
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

from services.formatting import fmt_money, pluralize_leads, truncate_at_word_boundary
from services.trend_policy import (
    REASON_DELTA_BELOW_NOISE,
    REASON_NO_MATURE_WEEK,
    REASON_NOT_SUSTAINED,
    REASON_PREV_WEEK_MISSING,
    REASON_VOLUME_BELOW_MIN,
    REASON_WEEK_NOT_COMPARABLE,
    REASON_WILSON_OVERLAP,
    SustainProof,
    TrendLevel,
    TrendStatus,
    TrendVerdict,
    TrendWeek,
    evaluate_cohort_rows,
)

logger = logging.getLogger(__name__)

# Таймзона CityA — в ней считаются недели когорт (дата создания лида).
_TZ_LOCAL = timezone(timedelta(hours=5))

# Source-specific колонки честного 7d-окна (миграция 015, Wave 3A). Читаются
# ЗАЩИЩЁННО через PRAGMA: если БД ещё без миграции 015 (старая схема / тестовая
# фикстура), их просто нет в SELECT, а в ad-dict попадут как None. NULL/complete=0
# = «нет подтверждённых полных 7d-данных», НЕ ноль оплат.
_SEVEN_D_COLUMNS = (
    "payments_amo_7d", "revenue_amo_7d",
    "amo_7d_window_from", "amo_7d_window_to", "amo_7d_synced_at", "amo_7d_complete",
    "payments_erp_7d", "revenue_erp_7d",
    "erp_7d_window_from", "erp_7d_window_to", "erp_7d_synced_at", "erp_7d_complete",
)


# ---------------------------------------------------------------------------
# Сбор активных объявлений из локальной БД (быстро, без FB/AMO-сети)
# ---------------------------------------------------------------------------

def _fetch_ads_from_local_db() -> list[dict]:
    """Достаёт активные объявления из creative_kb + ранние лиды из ad_daily_metrics.

    Два SQL-запроса, никаких сетевых вызовов. Время: < 1 секунды.

    Запрос 1: SELECT из creative_kb WHERE effective_status='ACTIVE'
              (НЕ status — там все записи конфига, effective_status отражает реальное
               состояние от FB: активных объявлений на порядки меньше).
              Поля: только те что гарантированно есть по PRAGMA в рабочей схеме
              (spend, leads, qual_pct, romi, cpl, ctr, hook_rate, impressions,
               video_p25, video_p100, video_views_3s, qual_leads,
               ad_name, city, adset_type, days_running).
              adset_id в creative_kb отсутствует — ставим None; guardrail безопасно
              пропускает записи с пустым/None adset_id.
    Запрос 2: SELECT ad_id, SUM(leads) FROM ad_daily_metrics WHERE day_since_launch<=3
              AND ad_id IN (…активные ids…) — ранние лиды одним IN-запросом.

    Returns:
        Список dict в формате score_and_decide (ключи ad_id, ad_name, …).
        Пустой список если БД недоступна или записей нет.
    """
    from services.creative_intelligence import DB_PATH

    if DB_PATH is None:
        logger.warning("_fetch_ads_from_local_db: DB_PATH не инициализирован")
        return []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # доступ по имени колонки

    try:
        # Какие из 7d-колонок (миграция 015) реально есть в схеме — читаем защищённо
        # (старая схема / тестовая фикстура без миграции 015 → колонок нет, не падаем).
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(creative_kb)")}
        seven_d_present = [c for c in _SEVEN_D_COLUMNS if c in existing_cols]

        # Запрос 1: активные объявления из creative_kb по effective_status
        # effective_status добавлен миграцией 008 и обновляется creative_backfill/autopilot.
        # Запрашиваем только колонки, гарантированно существующие по реальной схеме проде.
        base_cols = [
            "ad_id", "ad_name", "city", "adset_type", "spend", "leads",
            "qual_pct", "romi", "cpl", "ctr", "hook_rate", "impressions",
            "video_p25", "video_p100", "video_views_3s", "qual_leads",
            "payments", "outcomes_matched_at", "days_running",
            "payments_erp", "revenue_erp_lcy", "payments_erp_synced_at",
        ]
        select_cols = base_cols + seven_d_present
        kb_rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM creative_kb WHERE effective_status = 'ACTIVE'"
        ).fetchall()

        if not kb_rows:
            logger.info("_fetch_ads_from_local_db: нет активных объявлений в creative_kb (effective_status='ACTIVE')")
            return []

        # Строим список dict — нормализованные ключи для score_and_decide
        ads: list[dict] = []
        ad_ids: list[str] = []
        for row in kb_rows:
            ad = {
                "ad_id":          row["ad_id"],
                "ad_name":        row["ad_name"] or "",
                "city":           row["city"] or "",
                "adset_type":     row["adset_type"] or "",
                # adset_id нет в creative_kb; None → guardrail в score_and_decide
                # безопасно пропустит (строка `if not adset_id: continue`)
                "adset_id":       None,
                "spend":          float(row["spend"] or 0),
                "leads":          int(row["leads"] or 0),
                "qual_pct":       row["qual_pct"],      # None допустимо
                "romi":           row["romi"],           # None допустимо
                "cpl":            float(row["cpl"] or 0),
                "ctr":            float(row["ctr"] or 0),
                "hook_rate":      row["hook_rate"],      # None допустимо
                "impressions":    int(row["impressions"] or 0),
                "video_p25":      int(row["video_p25"] or 0),
                "video_p100":     int(row["video_p100"] or 0),
                "video_views_3s": int(row["video_views_3s"] or 0),
                # payments нужен для правила confirmed_waster в decision_policy
                # None = нет данных (не паузим), 0 = есть лиды/расход но оплат нет
                "payments":       row["payments"],  # None допустимо
                # outcomes_matched_at нужен правилу confirmed_waster (B1):
                # None = сверка с AMO не проводилась → не паузим как слив
                "outcomes_matched_at": row["outcomes_matched_at"],
                "days_running":   int(row["days_running"] or 0),
                # Шаг B (ARCH-cdp-payments): реальные платежи ERP из CDP.
                # None = нет данных ERP по объявлению (не сверено / нет платежей
                # в окне) — НЕ «точно 0». Используется через payments_effective
                # (services.cdp_payments) под переключателем cdp.payments_source.
                "payments_erp":            row["payments_erp"],       # None допустимо
                "revenue_erp_lcy":         row["revenue_erp_lcy"],    # None допустимо
                "payments_erp_synced_at":  row["payments_erp_synced_at"],  # None допустимо
                # Поля нужные apply_portfolio_decisions (вызывается внутри score_and_decide)
                "effective_status": "ACTIVE",
                "recommendation":   "ЖДАТЬ",
                "reason":           "",
            }
            # Честные 7d-поля (миграция 015): присутствующие — из строки, отсутствующие
            # (старая схема) — None. NULL/complete=0 = «нет подтверждённых полных
            # 7d-данных» (services.cdp_payments.*_fresh_complete интерпретирует).
            for col in _SEVEN_D_COLUMNS:
                ad[col] = row[col] if col in seven_d_present else None
            ads.append(ad)
            ad_ids.append(row["ad_id"])

        # Запрос 2: ранние лиды (day_since_launch <= 3) из ad_daily_metrics
        # Один IN-запрос по всем активным id разом — без цикла
        placeholders = ",".join("?" * len(ad_ids))
        early_rows = conn.execute(
            f"""
            SELECT ad_id, SUM(leads) AS early_leads
            FROM ad_daily_metrics
            WHERE ad_id IN ({placeholders})
              AND day_since_launch <= 3
              AND lead_semantics_version = 2
              AND lead_parse_status IN ('ok', 'component_mismatch')
            GROUP BY ad_id
            """,
            ad_ids,
        ).fetchall()

        # Карта ad_id → early_leads
        early_by_id: dict[str, int] = {
            row["ad_id"]: int(row["early_leads"] or 0)
            for row in early_rows
        }

        # Подставляем early_leads в каждый ад
        for ad in ads:
            if ad["ad_id"] in early_by_id:
                ad["early_leads"] = early_by_id[ad["ad_id"]]
            # Иначе поле отсутствует — decision_policy обработает через days_running+leads

        logger.info(
            "_fetch_ads_from_local_db: %d активных объявлений, %d с ранними лидами",
            len(ads), len(early_by_id),
        )
        return ads

    except Exception as exc:
        logger.error("_fetch_ads_from_local_db: ошибка БД — %s", exc)
        return []

    finally:
        conn.close()


# build_shadow_recommendations / format_shadow_report / send_shadow_report /
# should_send_shadow_report / _load_shadow_state / _save_shadow_state удалены
# вместе с рассылкой «🔮 Тень» — они существовали
# только ради построения и отправки Telegram-сообщения теневого режима.


# ---------------------------------------------------------------------------
# Теневая секция тренда (волна 2 недельных когорт)
# ---------------------------------------------------------------------------
#
# Что бы сказал тренд по каждому адсету — и ничего больше. Функции ниже только
# читают ad_weekly_cohorts, прогоняют чистое правило services/trend_policy.py и
# рендерят текст. Ни предложений, ни мутаций, ни записи решений, ни отправки:
# встраивание в решения — волна 3. Отправляющей функции здесь намеренно нет.

# Сколько последних недель тянуть из когорт: правилу нужны три закрытые
# дозревшие недели (W-3..W-1) плюс запас на текущую и на ещё не дозревшую.
_TREND_LOOKBACK_WEEKS = 6
# Сколько блоков показывать в сообщении (лимит Telegram — 4096 символов).
_TREND_MAX_BLOCKS = 8

# Денежные колонки когорт (миграция 025). Читаются защищённо через PRAGMA —
# на БД без миграции их нет, и выручка тогда просто неизвестна.
_COHORT_REVENUE_COLUMNS = (
    "revenue_lcy", "revenue_horizon_days", "revenue_mature", "usd_lcy_rate",
)

_TREND_STATUS_EMOJI = {
    TrendStatus.GROWTH: "📈",
    TrendStatus.DECLINE: "📉",
    TrendStatus.PLATEAU: "➖",
    TrendStatus.INSUFFICIENT: "❔",
}

# Машинный код причины → человеческая фраза. Презентация живёт здесь, а не в
# правиле: trend_policy не должен знать про Telegram.
_TREND_REASON_RU = {
    REASON_NO_MATURE_WEEK: "нет дозревшей закрытой недели",
    REASON_PREV_WEEK_MISSING: "нет соседней недели для сравнения",
    REASON_WEEK_NOT_COMPARABLE: "неполные данные недели",
    REASON_VOLUME_BELOW_MIN: "меньше 20 лидов в неделю",
    REASON_DELTA_BELOW_NOISE: "разница в пределах недельного шума",
    REASON_WILSON_OVERLAP: "разница не подтверждена объёмом",
    REASON_NOT_SUSTAINED: "разовое движение, соседние недели не подтверждают",
}


def _fetch_cohort_rows_from_local_db(
    *,
    as_of: date,
    weeks: int = _TREND_LOOKBACK_WEEKS,
) -> list[dict]:
    """Читает недельные когорты за последние `weeks` недель. Один SQL по индексу.

    Отсутствие таблицы (БД без миграции 024 / старая фикстура) — не ошибка:
    возвращается пустой список, как и при недоступной БД. Границей берётся
    календарная дата, а не понедельник: week_start хранится ISO-строкой и
    сравнивается лексикографически, а лишняя неделя правилу не мешает.
    """
    from services.creative_intelligence import DB_PATH

    if DB_PATH is None:
        logger.warning("_fetch_cohort_rows_from_local_db: DB_PATH не инициализирован")
        return []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("ad_weekly_cohorts",),
        ).fetchone()
        if table is None:
            logger.info(
                "_fetch_cohort_rows_from_local_db: таблицы ad_weekly_cohorts нет "
                "(миграция 024 не применена) — тренд не считаем"
            )
            return []
        # Денежные колонки добавлены миграцией 025 (волна 4). Читаем их так же
        # защищённо, как 7d-колонки выше: на БД без 025 их просто нет в SELECT,
        # и правило тренда увидит выручку как неизвестную, а не упадёт.
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(ad_weekly_cohorts)")
        }
        revenue_cols = [
            col for col in _COHORT_REVENUE_COLUMNS if col in existing_cols
        ]
        select_cols = [
            "ad_id", "ad_name", "adset_id", "adset_name", "week_start",
            "spend_usd", "amo_leads", "quals", "comparable",
            "not_comparable_reason",
        ] + revenue_cols
        cutoff = (as_of - timedelta(days=7 * weeks)).isoformat()
        rows = conn.execute(
            f"""
            SELECT {', '.join(select_cols)}
            FROM ad_weekly_cohorts
            WHERE week_start >= ?
            ORDER BY week_start, ad_id
            """,
            (cutoff,),
        ).fetchall()
        return [dict(row) for row in rows]
    except Exception as exc:
        logger.error("_fetch_cohort_rows_from_local_db: ошибка БД — %s", exc)
        return []
    finally:
        conn.close()


def build_trend_shadow(
    *,
    as_of: date | None = None,
    level: TrendLevel = TrendLevel.ADSET,
    weeks: int = _TREND_LOOKBACK_WEEKS,
) -> list[TrendVerdict]:
    """Вердикты тренда по адсетам (или по объявлениям) — только чтение.

    Порядок вывода: сперва то, на чём вообще можно было бы действовать
    (action_grade), затем по величине дельты, затем по имени. МАЛО ДАННЫХ
    остаётся в списке — форматтер сворачивает такие строки в одну сводку.
    """
    as_of = as_of or datetime.now(_TZ_LOCAL).date()
    rows = _fetch_cohort_rows_from_local_db(as_of=as_of, weeks=weeks)
    if not rows:
        return []

    verdicts = evaluate_cohort_rows(rows, level=level, as_of=as_of)
    verdicts.sort(
        key=lambda v: (
            not v.action_grade,
            -abs(v.delta_pp or 0.0),
            (v.entity_name or v.entity_id).lower(),
        )
    )
    logger.info(
        "build_trend_shadow: уровень %s, строк когорт %d, сущностей %d — "
        "%s (action-grade %d)",
        level.value,
        len(rows),
        len(verdicts),
        {
            status.value: sum(1 for v in verdicts if v.status is status)
            for status in TrendStatus
        },
        sum(1 for v in verdicts if v.action_grade),
    )
    return verdicts


def format_trend_shadow(
    verdicts: list[TrendVerdict],
    *,
    level: TrendLevel = TrendLevel.ADSET,
) -> str:
    """Рендерит теневую секцию тренда (эталон — _format_live_pause_block).

    Деньги — через fmt_money, None — «нет данных» (не 0 и не «—»), между
    блоками пустая строка. Строки МАЛО ДАННЫХ не занимают блок: они
    сворачиваются в одну строку с разбивкой по причинам.
    """
    scope = "адсетов" if level is TrendLevel.ADSET else "объявлений"
    header = f"🔭 <b>Тренд {scope}</b> — тень: смотрю, ничего не предлагаю и не меняю"
    if not verdicts:
        return f"{header}\nНедельных когорт нет — сравнивать нечего."

    judged = [v for v in verdicts if v.status is not TrendStatus.INSUFFICIENT]
    unjudged = [v for v in verdicts if v.status is TrendStatus.INSUFFICIENT]

    subtitle = (
        "Сравниваю дозревшие закрытые недели по квал-проценту; "
        "текущая неделя не участвует. ROMI когорты — справочно: порог шума "
        "для него не замерен, вердикт он не меняет."
    )
    if level is not TrendLevel.ADSET:
        subtitle += " Уровень объявления — диагностика, действий не даёт."

    parts = [header, subtitle]
    if judged:
        shown = judged[:_TREND_MAX_BLOCKS]
        parts.append("")
        parts.append(
            "\n\n".join(
                _format_trend_block(index, verdict)
                for index, verdict in enumerate(shown, start=1)
            )
        )
        if len(judged) > len(shown):
            parts.append(f"\n…и ещё {len(judged) - len(shown)} (см. дашборд)")
    if unjudged:
        parts.append(f"\n❔ Мало данных: {_format_insufficient_summary(unjudged)}")
    return "\n".join(parts)


def _format_trend_block(index: int, verdict: TrendVerdict) -> str:
    """Один блок теневой секции: статус, недели, объёмы, чем подтверждено."""
    name = truncate_at_word_boundary(
        verdict.entity_name or verdict.entity_id, 60
    )
    emoji = _TREND_STATUS_EMOJI[verdict.status]
    recent, previous = verdict.recent, verdict.previous

    qual_line = (
        f"квал {_fmt_pct(previous.qual_pct if previous else None)} → "
        f"{_fmt_pct(recent.qual_pct if recent else None)} "
        f"({_fmt_pp(verdict.delta_pp)})"
    )
    weeks_line = (
        f"недели {_fmt_week_range(previous)} → {_fmt_week_range(recent)}"
        f" · возраст {recent.age_days} дн"
        if recent is not None and previous is not None
        else "недели: нет данных"
    )
    leads_line = (
        f"{_fmt_leads_pair(previous, recent)} · "
        f"💸 {fmt_money(previous.spend_usd if previous else None, '$')} → "
        f"{fmt_money(recent.spend_usd if recent else None, '$')} · "
        f"CPL {fmt_money(previous.cpl_usd if previous else None, '$')} → "
        f"{fmt_money(recent.cpl_usd if recent else None, '$')}"
    )
    return (
        f"{index}. {html.escape(name)}\n"
        f"   {emoji} {verdict.status.value} · {qual_line}\n"
        f"   📅 {weeks_line}\n"
        f"   👥 {leads_line}\n"
        f"   💰 {_format_trend_romi(verdict)}\n"
        f"   🧪 {html.escape(_format_trend_proof(verdict))}"
    )


def _format_trend_romi(verdict: TrendVerdict) -> str:
    """Строка ROMI со зрелостью когорты: «дозрела» или «ещё зреет N из H дней».

    ROMI здесь справочный — статуса он не меняет (порог шума для него не
    замерен), и строка об этом прямо говорит. Незрелая когорта показывает не
    «0%», а сколько дней горизонта прошло: пустая касса первой недели — это
    «деньги ещё не пришли», а не «объявление не окупается».
    """
    recent, previous = verdict.recent, verdict.previous
    maturity = _fmt_revenue_maturity(recent)
    if recent is None or recent.romi_pct is None:
        return f"ROMI: нет данных · {maturity}"
    trail = (
        f"{_fmt_romi(previous.romi_pct if previous else None)} → "
        f"{_fmt_romi(recent.romi_pct)}"
    )
    change = (
        f" ({_fmt_pp(verdict.romi_change_pp)})"
        if verdict.romi_change_pp is not None
        else ""
    )
    revenue = fmt_money(recent.revenue_lcy, "¤")
    return f"ROMI {trail}{change} · выручка {revenue} · {maturity} · справочно"


def _fmt_revenue_maturity(week: TrendWeek | None) -> str:
    """Состояние выручки недели словами, без подмены одного другим.

    Четыре разных состояния, и они не должны сливаться в одно «нет данных»:
    когорта дозрела и деньги посчитаны; горизонт закрыт, но ERP молчит; когорта
    ещё зреет (сколько дней прошло, если горизонт известен); недели нет вовсе.
    """
    if week is None:
        return "зрелость выручки: нет данных"
    horizon = week.revenue_horizon_days
    if week.revenue_mature:
        if week.revenue_lcy is None:
            return "горизонт закрыт, но выручки из ERP нет"
        if horizon is None:
            return "выручка дозрела"
        return f"выручка дозрела: {horizon} из {horizon} дней"
    if horizon is None:
        return "когорта ещё зреет"
    elapsed = week.revenue_days_elapsed
    elapsed_text = "нет данных" if elapsed is None else str(elapsed)
    return f"когорта ещё зреет: {elapsed_text} из {horizon} дней"


def _fmt_romi(value: float | None) -> str:
    """ROMI целым процентом. None — «нет данных», а не 0%."""
    return "нет данных" if value is None else f"{value:.0f}%"


def _format_trend_proof(verdict: TrendVerdict) -> str:
    """Строка «чем подтверждено» — или почему подтверждения нет."""
    floor = _fmt_number(verdict.noise_floor_pp)
    if verdict.sustained_by is SustainProof.THREE_WEEKS and verdict.earlier:
        path = (
            f"{_fmt_pct(verdict.earlier.qual_pct)} → "
            f"{_fmt_pct(verdict.previous.qual_pct if verdict.previous else None)} → "
            f"{_fmt_pct(verdict.recent.qual_pct if verdict.recent else None)}"
        )
        text = f"три недели подряд в одну сторону: {path} · порог шума {floor} п.п."
    elif verdict.sustained_by is SustainProof.MARGIN:
        text = f"разовое движение вдвое выше порога шума {floor} п.п."
    else:
        text = _trend_reason_ru(verdict.reason)
        if verdict.delta_pp is not None and verdict.noise_floor_pp is not None:
            text += f" ({_fmt_number(abs(verdict.delta_pp))} из {floor} п.п.)"
    if verdict.status in (TrendStatus.GROWTH, TrendStatus.DECLINE) and (
        not verdict.action_grade
    ):
        text += " · только диагностика: уровень объявления действий не даёт"
    return text


def _format_insufficient_summary(verdicts: list[TrendVerdict]) -> str:
    """«12 — меньше 20 лидов в неделю 7, неполные данные недели 5» — свод."""
    counts: dict[str, int] = {}
    for verdict in verdicts:
        phrase = _trend_reason_ru(verdict.reason)
        counts[phrase] = counts.get(phrase, 0) + 1
    detail = ", ".join(
        f"{phrase} {count}"
        for phrase, count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )
    )
    return f"{len(verdicts)} — {detail}"


def _trend_reason_ru(reason: str) -> str:
    """Код причины (возможно с ':деталью') → фраза. Неизвестный код — как есть."""
    code = (reason or "").split(":", 1)[0]
    return _TREND_REASON_RU.get(code, code or "причина не указана")


def _fmt_pct(value: float | None) -> str:
    """Квал-процент целым числом. None — «нет данных», а не 0%."""
    return "нет данных" if value is None else f"{value:.0f}%"


def _fmt_pp(value: float | None) -> str:
    """Дельта в процентных пунктах со знаком: «-12,0 п.п.»."""
    if value is None:
        return "разница: нет данных"
    return f"{'+' if value > 0 else ''}{_fmt_number(value)} п.п."


def _fmt_number(value: float | None) -> str:
    """Число с одним знаком после запятой по-русски (запятая, не точка)."""
    return "нет данных" if value is None else f"{value:.1f}".replace(".", ",")


def _fmt_leads_pair(previous: TrendWeek | None, recent: TrendWeek | None) -> str:
    """«120 → 96 лидов». Неизвестный объём — «нет данных», а не 0."""
    if previous is None or previous.leads is None:
        return "лиды: нет данных"
    if recent is None or recent.leads is None:
        return f"{previous.leads} {pluralize_leads(previous.leads)} → нет данных"
    return f"{previous.leads} → {recent.leads} {pluralize_leads(recent.leads)}"


def _fmt_week_range(week: TrendWeek | None) -> str:
    """«29.06–05.07» — границы недели так, как их читает владелец."""
    if week is None:
        return "нет данных"
    return (
        f"{week.week_start.strftime('%d.%m')}–{week.week_end.strftime('%d.%m')}"
    )
