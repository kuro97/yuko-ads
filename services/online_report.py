"""Ежедневный онлайн-отчёт из CDP — один Telegram по псевдогороду «Онлайн».

Крон 10:3x CityA (после ночного обновления CDP), дедуп по дню. Читает у CDP
daily-report только строку city=="Онлайн":

- за вчера — расход, лиды, квалы (%), ДРР дня;
- за месяц с 1-го по вчера — суммарный расход и ДРР месяца.

ДРР месяца. Если в строках есть revenue_new + usd_rate — считаем строго как
compute_cdp_unit_economics: Σ(ad_spend×usd_rate)/Σ(revenue_new). Если revenue-поля
в строке нет (боевой daily-report Онлайн отдаёт готовые ad_spend/leads/qleads/
pct_qleads/drr_new/drr_full/usd_rate, БЕЗ revenue_new) — берём средневзвешенный
по расходу drr_new с пометкой «средневзв.».

CDP лёг → честная строка «CDP недоступен — онлайн-отчёт пропущен» ОДИН раз в день
(антиспам), не молчим и не падаем. Выключатель — autopilot.online_report.enabled.
Read-only: только GET CDP + Telegram, никаких мутаций.
"""

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from services import cdp_client, state_store
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
    ReportVerdict,
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
from services.cdp_client import CdpError
from services.formatting import fmt_money

logger = logging.getLogger(__name__)

_TZ_LOCAL = timezone(timedelta(hours=5))

# State: last_sent_date / last_unavailable_date — дневной антиспам.
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "online_report_state.json"

# Точное имя строки псевдогорода в CDP (не «Online»/«онлайн» — как в cdp_spend_alerts)
_ONLINE_CITY = "Онлайн"

# Дефолты настроек (мержатся поверх autopilot.online_report)
ONLINE_REPORT_DEFAULTS: dict = {"enabled": True}


def get_online_report_config() -> dict:
    """autopilot.online_report из settings.json поверх дефолтов (плоский мерж чинит хвост)."""
    from services.autopilot import get_autopilot_config

    cfg = get_autopilot_config().get("online_report") or {}
    return {**ONLINE_REPORT_DEFAULTS, **cfg}


# ---------------------------------------------------------------------------
# Разбор строк CDP
# ---------------------------------------------------------------------------

def _f(value: object) -> float:
    """float(value) или 0.0 (None/битое → 0.0)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _find_online_row(items: list, day: date) -> dict | None:
    """Строка city=='Онлайн' за конкретный день (точное имя, дата совпадает)."""
    for it in items or []:
        if not isinstance(it, dict) or it.get("city") != _ONLINE_CITY:
            continue
        raw = it.get("report_date")
        try:
            parsed = raw if isinstance(raw, date) else date.fromisoformat(raw)
        except (TypeError, ValueError):
            continue
        if parsed == day:
            return it
    return None


def _month_online_rows(items: list) -> list[dict]:
    """Все строки city=='Онлайн' (окно уже отфильтровано get_daily_report)."""
    return [it for it in (items or []) if isinstance(it, dict) and it.get("city") == _ONLINE_CITY]


def _compute_month_drr(rows: list[dict]) -> tuple[float | None, bool]:
    """ДРР месяца в процентах. Возвращает (drr_pct | None, weighted_flag).

    weighted_flag=True → считали средневзвешенный drr_new (в строках нет revenue).
    """
    total_spend = sum(_f(r.get("ad_spend")) for r in rows)
    if total_spend <= 0:
        return None, False

    has_revenue = bool(rows) and all(
        r.get("revenue_new") is not None and r.get("usd_rate") is not None for r in rows
    )
    if has_revenue:
        spend_lcy = sum(_f(r.get("ad_spend")) * _f(r.get("usd_rate")) for r in rows)
        revenue_lcy = sum(_f(r.get("revenue_new")) for r in rows)
        if revenue_lcy > 0:
            return spend_lcy / revenue_lcy * 100.0, False

    # Средневзвешенный по расходу готовый drr_new (drr_new — уже проценты)
    num = sum(_f(r.get("drr_new")) * _f(r.get("ad_spend")) for r in rows)
    return num / total_spend, True


def _fmt_pct(value: object) -> str:
    """Проценты без ложной точности: 30.0→«30%», 4.12→«4.1%», None→«н/д»."""
    if value is None:
        return "н/д"
    rounded = round(_f(value), 1)
    if rounded == int(rounded):
        return f"{int(rounded)}%"
    return f"{rounded:.1f}%"


def build_message(yesterday: date, yst_row: dict, month_rows: list[dict]) -> str:
    """Строит текст онлайн-отчёта (дословный формат ТЗ)."""
    spend = _f(yst_row.get("ad_spend"))
    leads = int(_f(yst_row.get("leads")))
    qleads = int(_f(yst_row.get("qleads")))
    pct_q = _fmt_pct(yst_row.get("pct_qleads"))
    drr_day = _fmt_pct(yst_row.get("drr_new"))

    month_spend = sum(_f(r.get("ad_spend")) for r in month_rows)
    month_drr, weighted = _compute_month_drr(month_rows)
    drr_month = _fmt_pct(month_drr)
    if weighted and month_drr is not None:
        drr_month += " (средневзв.)"

    return (
        f"🌐 {_ONLINE_CITY} (CDP)\n\n"
        f"Вчера {yesterday.strftime('%d.%m')}: расход {fmt_money(spend)} · "
        f"лидов {leads} · квалов {qleads} ({pct_q}) · ДРР дня {drr_day}\n"
        f"Месяц с 1.{yesterday.strftime('%m')}: расход {fmt_money(month_spend)} · "
        f"ДРР месяца {drr_month}\n\n"
        "(разрез PRODA/PRODB появится после доработки CDP)"
    )


def _strict_decimal(value: object, field_name: str) -> Decimal:
    """Преобразует обязательное число без подмены отсутствия нулём."""

    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name}: обязательное число отсутствует")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name}: некорректное число") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name}: число должно быть конечным")
    return parsed


def _strict_count(value: object, field_name: str) -> int:
    parsed = _strict_decimal(value, field_name)
    if parsed < 0 or parsed != parsed.to_integral_value():
        raise ValueError(f"{field_name}: ожидается целое неотрицательное число")
    return int(parsed)


def _compute_month_drr_checked(rows: list[dict]) -> tuple[Decimal, bool]:
    """Считает typed ДРР без silent zero и не смешивает два режима."""

    if not rows:
        raise ValueError("Строки Онлайн за месяц отсутствуют")
    total_spend = sum(
        (_strict_decimal(row.get("ad_spend"), "month.ad_spend") for row in rows),
        Decimal("0"),
    )
    if total_spend <= 0:
        raise ValueError("Месячный расход должен быть положительным")

    exact_revenue = all(
        row.get("revenue_new") is not None and row.get("usd_rate") is not None
        for row in rows
    )
    if exact_revenue:
        spend_lcy = Decimal("0")
        revenue_lcy = Decimal("0")
        for row in rows:
            spend = _strict_decimal(row.get("ad_spend"), "month.ad_spend")
            usd_rate = _strict_decimal(row.get("usd_rate"), "month.usd_rate")
            revenue = _strict_decimal(row.get("revenue_new"), "month.revenue_new")
            if usd_rate <= 0 or revenue < 0:
                raise ValueError("usd_rate/revenue_new вне допустимого диапазона")
            spend_lcy += spend * usd_rate
            revenue_lcy += revenue
        if revenue_lcy <= 0:
            raise ValueError("При exact-режиме выручка должна быть положительной")
        return spend_lcy / revenue_lcy * Decimal("100"), False

    weighted_sum = Decimal("0")
    for row in rows:
        spend = _strict_decimal(row.get("ad_spend"), "month.ad_spend")
        drr = _strict_decimal(row.get("drr_new"), "month.drr_new")
        weighted_sum += spend * drr
    return weighted_sum / total_spend, True


def _field_and_claim(
    *,
    field_id: str,
    section_id: str,
    category: FactCategory,
    label: FieldLabelTemplate,
    field_format: FieldFormat,
    subject: SubjectRef,
    metric: Metric,
    value: int | Decimal | str | None,
    window: TimeWindow,
    currency: str | None = None,
) -> tuple[ReportField, FactClaim]:
    field = ReportField(
        field_id=field_id,
        section_id=section_id,
        category=category,
        label=label,
        format=field_format,
        subject=subject,
        metric=metric,
        value=value,
        source=SourceSystem.CDP_ERP,
        window=window,
        currency=currency,
        required=True,
    )
    claim = FactClaim(
        claim_id=f"claim:{field_id}",
        field_id=field_id,
        category=category,
        subject=subject,
        metric=metric,
        value=value,
        source=SourceSystem.CDP_ERP,
        window=window,
        currency=currency,
        required=True,
    )
    return field, claim


def build_online_report_request(
    yesterday: date,
    yst_row: dict,
    month_rows: list[dict],
    *,
    generated_at: datetime,
) -> ReportCheckRequest:
    """Строит полный typed-манифест отчёта до независимой сверки CDP.

    Контекст даты и режима ДРР тоже является claim. Если текущий checker-source
    ещё не умеет независимо доказать поле, итог будет BLOCKED, а не частично
    доверенный текст с числами.
    """

    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at должен содержать timezone")
    day_start = datetime.combine(yesterday, datetime.min.time(), tzinfo=_TZ_LOCAL)
    day_window = TimeWindow(
        start=day_start,
        end=day_start + timedelta(days=1),
        timezone_name="Etc/GMT-5",
        semantic="online_daily_report",
    )
    month_start_date = yesterday.replace(day=1)
    month_start = datetime.combine(month_start_date, datetime.min.time(), tzinfo=_TZ_LOCAL)
    month_window = TimeWindow(
        start=month_start,
        end=day_window.end,
        timezone_name="Etc/GMT-5",
        semantic="online_month_to_date_report",
    )
    subject = SubjectRef(SubjectKind.ACCOUNT, "ONLINE")

    online_month_rows = _month_online_rows(month_rows)
    if not online_month_rows:
        raise ValueError("CDP не вернул строки Онлайн за месяц")
    day_spend = _strict_decimal(yst_row.get("ad_spend"), "ad_spend")
    day_leads = _strict_count(yst_row.get("leads"), "leads")
    day_quals = _strict_count(yst_row.get("qleads"), "qleads")
    day_qual_pct = _strict_decimal(yst_row.get("pct_qleads"), "pct_qleads")
    day_drr = _strict_decimal(yst_row.get("drr_new"), "drr_new")
    month_spend = sum(
        (_strict_decimal(row.get("ad_spend"), "month.ad_spend") for row in online_month_rows),
        Decimal("0"),
    )
    month_drr, weighted = _compute_month_drr_checked(online_month_rows)

    fields: list[ReportField] = []
    claims: list[FactClaim] = []

    def add(**kwargs: object) -> None:
        field, claim = _field_and_claim(**kwargs)  # type: ignore[arg-type]
        fields.append(field)
        claims.append(claim)

    add(
        field_id="online.day.date",
        section_id="daily",
        category=FactCategory.DISPLAY_CONTEXT,
        label=FieldLabelTemplate.DISPLAY_NAME,
        field_format=FieldFormat.TEXT,
        subject=subject,
        metric=Metric.DISPLAY_CONTEXT,
        value=yesterday.isoformat(),
        window=day_window,
    )
    for field_id, label, metric, value, field_format, currency in (
        ("online.day.spend", FieldLabelTemplate.SPEND, Metric.SPEND, day_spend, FieldFormat.USD, "USD"),
        ("online.day.leads", FieldLabelTemplate.LEADS, Metric.LEADS, day_leads, FieldFormat.INTEGER, None),
        ("online.day.quals", FieldLabelTemplate.QUALS, Metric.QUALS, day_quals, FieldFormat.INTEGER, None),
        ("online.day.qual_pct", FieldLabelTemplate.MATCH, Metric.QUAL_PCT, day_qual_pct, FieldFormat.PERCENT, None),
        ("online.day.drr", FieldLabelTemplate.DRR, Metric.DRR_PCT, day_drr, FieldFormat.PERCENT, None),
    ):
        add(
            field_id=field_id,
            section_id="daily",
            category=FactCategory.BUSINESS_METRIC,
            label=label,
            field_format=field_format,
            subject=subject,
            metric=metric,
            value=value,
            window=day_window,
            currency=currency,
        )

    add(
        field_id="online.month.period",
        section_id="month",
        category=FactCategory.DISPLAY_CONTEXT,
        label=FieldLabelTemplate.DISPLAY_NAME,
        field_format=FieldFormat.TEXT,
        subject=subject,
        metric=Metric.DISPLAY_CONTEXT,
        value=f"{month_start_date.isoformat()}..{yesterday.isoformat()}",
        window=month_window,
    )
    add(
        field_id="online.month.spend",
        section_id="month",
        category=FactCategory.BUSINESS_METRIC,
        label=FieldLabelTemplate.SPEND,
        field_format=FieldFormat.USD,
        subject=subject,
        metric=Metric.SPEND,
        value=month_spend,
        window=month_window,
        currency="USD",
    )
    add(
        field_id="online.month.drr",
        section_id="month",
        category=FactCategory.BUSINESS_METRIC,
        label=FieldLabelTemplate.DRR,
        field_format=FieldFormat.PERCENT,
        subject=subject,
        metric=Metric.DRR_PCT,
        value=month_drr,
        window=month_window,
    )
    add(
        field_id="online.month.drr_mode",
        section_id="month",
        category=FactCategory.DISPLAY_CONTEXT,
        label=FieldLabelTemplate.MATCH,
        field_format=FieldFormat.TEXT,
        subject=subject,
        metric=Metric.MATCH_STATE,
        value="WEIGHTED_FALLBACK" if weighted else "EXACT_REVENUE",
        window=month_window,
    )
    if not weighted:
        revenue = sum(
            (_strict_decimal(row.get("revenue_new"), "month.revenue_new") for row in online_month_rows),
            Decimal("0"),
        )
        add(
            field_id="online.month.revenue",
            section_id="month",
            category=FactCategory.BUSINESS_METRIC,
            label=FieldLabelTemplate.REVENUE,
            field_format=FieldFormat.LCY,
            subject=subject,
            metric=Metric.REVENUE,
            value=revenue,
            window=month_window,
            currency="LCY",
        )

    sections = (
        ReportSection(
            section_id="daily",
            template=SectionTemplate.OUTCOMES,
            field_ids=tuple(field.field_id for field in fields if field.section_id == "daily"),
            window=day_window,
        ),
        ReportSection(
            section_id="month",
            template=SectionTemplate.SUMMARY,
            field_ids=tuple(field.field_id for field in fields if field.section_id == "month"),
            window=month_window,
        ),
    )
    payload = TypedReportPayload(
        template=ReportTemplate.ONLINE,
        sections=sections,
        fields=tuple(fields),
        generated_at=generated_at,
        mixed_windows_explicit=True,
    )
    claim_tuple = tuple(claims)
    return ReportCheckRequest(
        correlation_id=f"online:{yesterday.isoformat()}:{uuid.uuid4()}",
        payload=payload,
        claims=claim_tuple,
        manifest_sha256=report_manifest_sha256(payload, claim_tuple),
    )


# ---------------------------------------------------------------------------
# State + отправка
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    return state_store.load_json_state(_STATE_FILE)


def _save_state(state: dict) -> None:
    try:
        state_store.save_json_state(_STATE_FILE, state)
    except Exception as exc:
        logger.error("online_report: не удалось сохранить state — %s", type(exc).__name__)


def _send_unavailable(error_type: str) -> bool:
    """Шлёт только закрытый технический шаблон без бизнес-данных."""

    try:
        delivery = send_fact_free(
            FactFreeTemplate.PROVIDER_UNAVAILABLE,
            channel="ads",
            error_type=error_type,
        )
        return delivery.sent
    except Exception as exc:
        logger.warning("online_report: checked Telegram exception — %s", type(exc).__name__)
        return False


def _check_render_send(request: ReportCheckRequest, *, now: datetime) -> tuple[bool, ReportVerdict]:
    """Единственный normal-delivery путь: check → render → checked facade."""

    result = check_report(request, now=now)
    rendered = render_checked_report(request, result)
    delivery = send_checked_report(rendered, channel="ads")
    return delivery.sent, result.verdict


def _normalize_moment(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_TZ_LOCAL)


# ---------------------------------------------------------------------------
# Основной прогон (never-throw)
# ---------------------------------------------------------------------------

def run_online_report(now: datetime | None = None) -> dict:
    """Собирает и шлёт онлайн-отчёт за вчера + месяц-до-вчера. Дедуп по дню.

    Never-throw. Возвращает {"target_date", "ok", "status", "sent"}. status:
      disabled | already_sent | sent | send_failed | no_data | cdp_unavailable |
      cdp_unavailable_deduped | error.
    """
    moment = _normalize_moment(now)
    yesterday = moment.date() - timedelta(days=1)
    today_iso = moment.date().isoformat()
    result: dict = {"target_date": yesterday.isoformat(), "ok": True, "status": "sent", "sent": False}

    try:
        if not get_online_report_config().get("enabled", True):
            result["status"] = "disabled"
            return result

        state = _load_state()
        if state.get("last_sent_date") == today_iso:
            result["status"] = "already_sent"
            return result

        month_start = yesterday.replace(day=1)
        try:
            daily_items = cdp_client.get_daily_report(yesterday, yesterday)
            month_items = cdp_client.get_daily_report(month_start, yesterday)
        except CdpError as exc:
            logger.warning("online_report: CDP недоступен — %s", type(exc).__name__)
            result["ok"] = False
            # Честная строка ОДИН раз в день (антиспам)
            if state.get("last_unavailable_date") == today_iso:
                result["status"] = "cdp_unavailable_deduped"
                return result
            if _send_unavailable(type(exc).__name__):
                state["last_unavailable_date"] = today_iso
                _save_state(state)
                result["status"] = "cdp_unavailable"
                result["sent"] = True
            else:
                result["status"] = "cdp_unavailable"
            return result

        yst_row = _find_online_row(daily_items, yesterday)
        if yst_row is None:
            # CDP жив, но строки Онлайн за вчера ещё нет — не шлём кривой отчёт,
            # не помечаем день (повторим на следующем тике/завтра).
            logger.info("online_report: нет строки «Онлайн» за %s — пропуск", yesterday)
            result["status"] = "no_data"
            return result

        month_rows = _month_online_rows(month_items)
        try:
            request = build_online_report_request(
                yesterday,
                yst_row,
                month_rows,
                generated_at=moment,
            )
        except (TypeError, ValueError) as exc:
            logger.warning("online_report: неполный typed snapshot — %s", type(exc).__name__)
            result["ok"] = False
            result["status"] = "blocked"
            if state.get("last_unavailable_date") != today_iso:
                result["sent"] = _send_unavailable(type(exc).__name__)
                if result["sent"]:
                    state["last_unavailable_date"] = today_iso
                    _save_state(state)
            return result
        sent, verdict = _check_render_send(request, now=moment)
        if not sent:
            result["ok"] = False
            result["status"] = "send_failed"
            return result

        if verdict in {ReportVerdict.BLOCKED, ReportVerdict.CHECKER_UNAVAILABLE}:
            state["last_unavailable_date"] = today_iso
            _save_state(state)
            result["ok"] = False
            result["sent"] = True
            result["status"] = "blocked"
            return result

        state["last_sent_date"] = today_iso
        _save_state(state)
        result["sent"] = True
        result["status"] = "sent"
        return result
    except Exception as exc:
        logger.warning("online_report: ошибка прогона — %s", type(exc).__name__)
        result["ok"] = False
        result["status"] = "error"
        return result
