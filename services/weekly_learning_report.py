"""
Недельный отчёт «чему я научился» Аналитика-Гипотезника (Фаза 4).

Раз в неделю (воскресенье вечер, web/app.py._cron_weekly_learning_report)
собирает человекочитаемую сводку: какие гипотезы подтвердились, какие
опровергнуты, топ-уроки, свежие предикторы pattern_engine и какие «мёртвые»
комбо угол×город×формат бот перестаёт пробовать (мягко, без исключения).

Каждая секция обёрнута в свой try/except — сбой одной секции (например,
pattern_engine недоступен) НЕ должен ронять весь отчёт. Текст — человеческим
языком на русском, HTML-разметка для Telegram, компактный (≤25 строк).

См. docs/specs/ARCH-phase4-hypothesist.md §6.4, §8, T8.
"""

import html
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from services.approval_checker import check_report
from services.approval_checker_models import (
    FactCategory,
    FactClaim,
    FactFreeTemplate,
    FieldFormat,
    FieldLabelTemplate,
    Metric,
    ReportCheckRequest,
    ReportField,
    ReportSection,
    ReportTemplate,
    SectionTemplate,
    SourceSystem,
    SubjectKind,
    SubjectRef,
    TimeWindow,
    TypedReportPayload,
    report_manifest_sha256,
)
from services.approval_report import render_checked_report
from services.approval_telegram import send_checked_report, send_fact_free

logger = logging.getLogger(__name__)

# Сколько дней назад считать «неделей» для выборки вердиктов/уроков
_WEEK_DAYS = 7

# Сколько активных уроков source='hypothesis' показывать в топе
_TOP_LEARNINGS_LIMIT = 3

# Сколько подтверждённых/опровергнутых гипотез показывать построчно (защита от простыни)
_MAX_ITEMS_PER_SECTION = 5

# Сколько предикторов pattern_engine показывать
_MAX_PREDICTORS = 3

# Сколько мёртвых комбо показывать в «что меняем»
_MAX_DEAD_COMBOS = 5


def _week_cutoff_iso(now: datetime) -> str:
    """ISO-строка отсечки «неделя назад» для SQL-фильтров verdict_at/created_at."""
    cutoff = now - timedelta(days=_WEEK_DAYS)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def _section_confirmed(now: datetime) -> list[str]:
    """Секция 1: подтверждённые гипотезы недели (verdict_at за 7 дней)."""
    from services.hypothesis_journal import get_hypotheses_by_status

    since_iso = _week_cutoff_iso(now)
    hyps = get_hypotheses_by_status("confirmed", since_iso=since_iso)

    lines = ["<b>✅ Подтвердилось:</b>"]
    if not hyps:
        lines.append("на этой неделе подтверждений не было")
        return lines

    for hyp in hyps[:_MAX_ITEMS_PER_SECTION]:
        lesson = hyp.get("lesson") or f"{hyp.get('angle','')} в {hyp.get('city','')}"
        lines.append(f"• {html.escape(lesson)}")

    remaining = len(hyps) - _MAX_ITEMS_PER_SECTION
    if remaining > 0:
        lines.append(f"...и ещё {remaining}")

    return lines


def _section_refuted(now: datetime) -> list[str]:
    """Секция 2: опровергнутые гипотезы недели (verdict_at за 7 дней)."""
    from services.hypothesis_journal import get_hypotheses_by_status

    since_iso = _week_cutoff_iso(now)
    hyps = get_hypotheses_by_status("refuted", since_iso=since_iso)

    lines = ["<b>❌ Не подтвердилось:</b>"]
    if not hyps:
        lines.append("на этой неделе опровержений не было")
        return lines

    for hyp in hyps[:_MAX_ITEMS_PER_SECTION]:
        lesson = hyp.get("lesson") or f"{hyp.get('angle','')} в {hyp.get('city','')}"
        lines.append(f"• {html.escape(lesson)}")

    remaining = len(hyps) - _MAX_ITEMS_PER_SECTION
    if remaining > 0:
        lines.append(f"...и ещё {remaining}")

    return lines


def _section_top_learnings(now: datetime) -> list[str]:
    """Секция 3: топ-уроки source='hypothesis' за неделю (свежие, любой вердикт)."""
    from services.creative_intelligence import _get_connection

    since_iso = _week_cutoff_iso(now)
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT statement FROM learnings
            WHERE source = 'hypothesis' AND created_at >= ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (since_iso, _TOP_LEARNINGS_LIMIT),
        ).fetchall()
    finally:
        conn.close()

    lines = ["<b>📚 Топ-уроки недели:</b>"]
    if not rows:
        lines.append("эта неделя без новых уроков")
        return lines

    for row in rows:
        lines.append(f"• {html.escape(row['statement'])}")

    return lines


def _section_predictors() -> list[str]:
    """Секция 4: свежие предикторы pattern_engine.get_patterns_summary()."""
    from services.pattern_engine import get_patterns_summary

    summary = get_patterns_summary()

    lines = ["<b>🔮 Предикторы:</b>"]
    if summary.get("data_sufficiency") != "ok":
        lines.append("данных недостаточно")
        return lines

    predictors = summary.get("predictors") or []
    if not predictors:
        lines.append("данных недостаточно")
        return lines

    for pred in predictors[:_MAX_PREDICTORS]:
        statement = pred.get("statement") or pred.get("metric") or ""
        if statement:
            lines.append(f"• {html.escape(statement)}")

    return lines


def _section_what_changes(now: datetime) -> list[str]:
    """Секция 5: «что меняем» — мёртвые комбо из hypothesis_influence.load_verdict_weights."""
    from services.hypothesis_influence import load_verdict_weights

    weights = load_verdict_weights(now=now)
    dead_combos = [key for key, entry in weights.items() if entry.get("dead")]

    lines = ["<b>🔧 Что меняем:</b>"]
    if not dead_combos:
        lines.append("мёртвых комбо пока нет — продолжаем как есть")
        return lines

    for key in dead_combos[:_MAX_DEAD_COMBOS]:
        # combo_key формата "угол::город::формат" — делаем читаемым
        angle, city, ad_format = (key.split("::") + ["*", "*", "*"])[:3]
        parts = [p for p in (angle, city, ad_format) if p and p != "*"]
        readable = " / ".join(parts) if parts else key
        lines.append(f"• меньше пробуем: {html.escape(readable)}")

    remaining = len(dead_combos) - _MAX_DEAD_COMBOS
    if remaining > 0:
        lines.append(f"...и ещё {remaining}")

    return lines


def _section_products(now: datetime) -> list[str]:
    """Секция 6: «Какого продукта больше» — разбивка активных объявлений по
    продуктам (PRODA/PRODB/СТАРТ/ОБЩАЯ).

    Источник — creative_kb.target_product через product_report.build_product_breakdown
    (НЕ парсинг имён объявлений — «имя для человека, поле для машины», см.
    docs/specs/ARCH-product-tags.md §1, §8). Делает измеримой директиву
    владельца «лить PRODA и общую»: доля PRODA в активных объявлениях видна
    из недели в неделю прямо в этом отчёте.
    """
    from services.product_report import build_product_breakdown

    rows = build_product_breakdown(days=_WEEK_DAYS)

    lines = ["<b>📦 Какого продукта больше</b>"]
    total_ads = sum(row.get("ads_count", 0) for row in rows)
    if total_ads == 0:
        lines.append("активных объявлений с разметкой продукта пока нет")
        return lines

    parts = [f"{html.escape(str(row['product']))} {row['share_pct']:.0f}%" for row in rows]
    lines.append(" · ".join(parts))

    return lines


def build_weekly_report(now: datetime | None = None) -> str:
    """Собирает текст недельного отчёта (HTML для Telegram).

    Секции:
      1. Подтверждённые гипотезы недели.
      2. Опровергнутые гипотезы недели.
      3. Топ-3 активных урока (source='hypothesis' за неделю).
      4. Свежие предикторы pattern_engine.
      5. «Что меняем»: мёртвые комбо из hypothesis_influence.
      6. «Какого продукта больше»: доля PRODA/PRODB/СТАРТ/ОБЩАЯ среди активных
         объявлений (product_report.build_product_breakdown, см.
         docs/specs/ARCH-product-tags.md).

    Каждая секция в своём try/except — сбой одной НЕ ломает отчёт целиком
    (вместо секции подставляется заглушка с ошибкой). Пустая БД → валидный
    текст с «пока нет» в каждой секции, функция никогда не бросает исключений.
    """
    now = now or datetime.now()

    lines = [f"<b>🧠 Чему я научился за неделю</b> ({now.strftime('%d.%m.%Y')})", ""]

    section_builders = [
        ("confirmed", lambda: _section_confirmed(now)),
        ("refuted", lambda: _section_refuted(now)),
        ("top_learnings", lambda: _section_top_learnings(now)),
        ("predictors", lambda: _section_predictors()),
        ("what_changes", lambda: _section_what_changes(now)),
        ("products", lambda: _section_products(now)),
    ]

    for name, builder in section_builders:
        try:
            section_lines = builder()
        except Exception as exc:
            logger.warning("weekly_learning_report: секция %s упала: %s", name, exc)
            section_lines = [f"<b>{name}:</b>", "временно недоступно"]
        lines.extend(section_lines)
        lines.append("")

    # Убираем хвостовую пустую строку
    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


def _aware_now(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _weekly_source_snapshot(now: datetime) -> dict[str, object]:
    """Повторяет producer-расчёт без silent defaults; checker перечитает БД сам."""

    from services.creative_intelligence import _get_connection
    from services.hypothesis_journal import get_hypotheses_by_status
    from services.product_report import build_product_breakdown

    since = now - timedelta(days=_WEEK_DAYS)
    since_iso = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_iso = now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    hypothesis_total = sum(
        len(get_hypotheses_by_status(status, since_iso=since_iso))
        for status in ("open", "confirmed", "refuted", "inconclusive")
    )

    conn = _get_connection()
    try:
        pattern_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM learnings
             WHERE created_at >= ? AND created_at < ?
            """,
            (since_iso, end_iso),
        ).fetchone()
        untagged_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
              FROM creative_kb
             WHERE status = 'ACTIVE'
               AND (target_product IS NULL OR TRIM(target_product) = '')
            """
        ).fetchone()
    finally:
        conn.close()
    if pattern_row is None or untagged_row is None:
        raise ValueError("Не удалось получить полный weekly snapshot")

    products = []
    for row in build_product_breakdown(days=_WEEK_DAYS):
        count = row.get("ads_count")
        share = row.get("share_pct")
        product = row.get("product")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Некорректный product ads_count")
        if count == 0:
            continue
        if not isinstance(product, str) or not product:
            raise ValueError("Некорректный product code")
        share_decimal = Decimal(str(share))
        if not share_decimal.is_finite():
            raise ValueError("Некорректная product share")
        products.append((product, share_decimal))

    return {
        "hypothesis_total": hypothesis_total,
        "pattern_total": int(pattern_row["cnt"]),
        "untagged": int(untagged_row["cnt"]),
        "products": tuple(products),
    }


def _weekly_field_claim(
    *,
    field_id: str,
    section_id: str,
    label: FieldLabelTemplate,
    field_format: FieldFormat,
    subject: SubjectRef,
    metric: Metric,
    value: int | Decimal,
    source: SourceSystem,
    window: TimeWindow,
) -> tuple[ReportField, FactClaim]:
    field = ReportField(
        field_id=field_id,
        section_id=section_id,
        category=FactCategory.BUSINESS_METRIC,
        label=label,
        format=field_format,
        subject=subject,
        metric=metric,
        value=value,
        source=source,
        window=window,
        required=True,
    )
    claim = FactClaim(
        claim_id=f"claim:{field_id}",
        field_id=field_id,
        category=field.category,
        subject=subject,
        metric=metric,
        value=value,
        source=source,
        window=window,
        required=True,
    )
    return field, claim


def build_weekly_report_request(now: datetime | None = None) -> ReportCheckRequest:
    """Создаёт typed weekly report только из повторно читаемых чисел."""

    moment = _aware_now(now)
    window = TimeWindow(
        start=moment - timedelta(days=_WEEK_DAYS),
        end=moment,
        timezone_name=str(moment.tzinfo),
        semantic="weekly_learning_exact_7d",
    )
    snapshot = _weekly_source_snapshot(moment)
    fields: list[ReportField] = []
    claims: list[FactClaim] = []

    def add(**kwargs: object) -> None:
        field, claim = _weekly_field_claim(**kwargs)  # type: ignore[arg-type]
        fields.append(field)
        claims.append(claim)

    add(
        field_id="weekly.hypotheses.total",
        section_id="learning",
        label=FieldLabelTemplate.ACTION_COUNT,
        field_format=FieldFormat.INTEGER,
        subject=SubjectRef(SubjectKind.HYPOTHESIS, "total"),
        metric=Metric.HYPOTHESIS_COUNT,
        value=snapshot["hypothesis_total"],
        source=SourceSystem.HYPOTHESIS_JOURNAL,
        window=window,
    )
    add(
        field_id="weekly.patterns.total",
        section_id="learning",
        label=FieldLabelTemplate.ACTION_COUNT,
        field_format=FieldFormat.INTEGER,
        subject=SubjectRef(SubjectKind.PATTERN, "total"),
        metric=Metric.PATTERN_COUNT,
        value=snapshot["pattern_total"],
        source=SourceSystem.PATTERN_LEARNINGS,
        window=window,
    )
    for product, share in snapshot["products"]:  # type: ignore[assignment]
        add(
            field_id=f"weekly.product.{product}.share",
            section_id="products",
            label=FieldLabelTemplate.PRODUCT_SHARE,
            field_format=FieldFormat.PERCENT,
            subject=SubjectRef(SubjectKind.PRODUCT, product),
            metric=Metric.PRODUCT_SHARE_PCT,
            value=share,
            source=SourceSystem.CREATIVE_KB,
            window=window,
        )
    add(
        field_id="weekly.product.untagged",
        section_id="products",
        label=FieldLabelTemplate.MATCH,
        field_format=FieldFormat.INTEGER,
        subject=SubjectRef(SubjectKind.PRODUCT, "UNTAGGED"),
        metric=Metric.RECORD_STATUS,
        value=snapshot["untagged"],
        source=SourceSystem.CREATIVE_KB,
        window=window,
    )

    sections = (
        ReportSection(
            section_id="learning",
            template=SectionTemplate.SUMMARY,
            field_ids=tuple(field.field_id for field in fields if field.section_id == "learning"),
            window=window,
        ),
        ReportSection(
            section_id="products",
            template=SectionTemplate.OUTCOMES,
            field_ids=tuple(field.field_id for field in fields if field.section_id == "products"),
            window=window,
        ),
    )
    payload = TypedReportPayload(
        template=ReportTemplate.WEEKLY_LEARNING,
        sections=sections,
        fields=tuple(fields),
        generated_at=moment,
        mixed_windows_explicit=False,
    )
    claim_tuple = tuple(claims)
    return ReportCheckRequest(
        correlation_id=f"weekly-learning:{uuid.uuid4()}",
        payload=payload,
        claims=claim_tuple,
        manifest_sha256=report_manifest_sha256(payload, claim_tuple),
    )


def send_weekly_learning_report(now: datetime | None = None) -> bool:
    """Строит typed-отчёт и шлёт только через Approval Checker.

    Гейт: если hypothesist.enabled == False — отчёт не строится и не шлётся,
    возвращает False (контур выключен, крон в web/app.py и так проверяет флаг
    заранее, но функция самодостаточна и безопасна при прямом вызове).
    Никогда не бросает исключений наружу — ловит и логирует (never-throw для крона).
    """
    try:
        from services.autopilot import get_autopilot_config

        cfg = get_autopilot_config().get("hypothesist", {})
        if not cfg.get("enabled", True):
            logger.info("weekly_learning_report: hypothesist.enabled=False — отчёт не шлём")
            return False

        request = build_weekly_report_request(now=now)
        result = check_report(request, now=request.payload.generated_at)
        rendered = render_checked_report(request, result)
        return send_checked_report(rendered, channel="ads").sent
    except Exception as exc:
        logger.warning("weekly_learning_report: ошибка при отправке отчёта: %s", exc)
        try:
            send_fact_free(
                FactFreeTemplate.CHECKER_INTERNAL_ERROR,
                channel="ads",
                error_type=type(exc).__name__,
            )
        except Exception as delivery_exc:
            logger.warning(
                "weekly_learning_report: safe notice не отправлен: %s",
                type(delivery_exc).__name__,
            )
        return False
