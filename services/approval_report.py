from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from decimal import Decimal
from enum import Enum

from services.approval_checker_models import (
    CheckIssue,
    ClaimState,
    CoverageResult,
    FactCategory,
    FieldFormat,
    FieldLabelTemplate,
    RenderedReport,
    ReportCheckRequest,
    ReportCheckResult,
    ReportTemplate,
    ReportVerdict,
    SectionTemplate,
    report_manifest_sha256,
)


_REPORT_TITLES: dict[ReportTemplate, str] = {
    ReportTemplate.AUTOPILOT: "Автопилот",
    ReportTemplate.LAUNCH: "Запуск рекламы",
    ReportTemplate.SCALE: "Изменение бюджета",
    ReportTemplate.EVENING: "Вечерний отчёт",
    ReportTemplate.MORNING: "Утренний отчёт",
    ReportTemplate.STATUS: "Статус системы",
    ReportTemplate.ADS: "Реклама",
    ReportTemplate.ACTION_RESULT: "Результат действия",
    ReportTemplate.ONLINE: "Онлайн-направление",
    ReportTemplate.WEEKLY_LEARNING: "Недельное обучение",
    ReportTemplate.SCORECARD: "Качество решений",
    ReportTemplate.SPEND_ALERT: "Контроль расходов",
    ReportTemplate.COVERAGE: "Покрытие данных",
    ReportTemplate.GUARDIAN: "Страж рекламы",
    ReportTemplate.ANOMALY: "Аномалия",
    ReportTemplate.CLEANER: "Чистильщик",
    ReportTemplate.BRIEF: "Бриф",
    ReportTemplate.HEALTH: "Состояние системы",
}

_SECTION_TITLES: dict[SectionTemplate, str] = {
    SectionTemplate.SUMMARY: "Итог",
    SectionTemplate.FACEBOOK: "Facebook",
    SectionTemplate.OUTCOMES: "Продажи",
    SectionTemplate.ACTIONS: "Действия",
    SectionTemplate.MATCHES: "Сверка",
    SectionTemplate.LIMITATIONS: "Ограничения",
}

_FIELD_LABELS: dict[FieldLabelTemplate, str] = {
    FieldLabelTemplate.DISPLAY_NAME: "Название",
    FieldLabelTemplate.MATCH: "Сверка",
    FieldLabelTemplate.STATUS: "Статус",
    FieldLabelTemplate.SPEND: "Расход",
    FieldLabelTemplate.LEADS: "Лиды",
    FieldLabelTemplate.QUALS: "Квалифицированные",
    FieldLabelTemplate.PAYMENTS: "Оплаты",
    FieldLabelTemplate.REVENUE: "Выручка",
    FieldLabelTemplate.CPL: "CPL",
    FieldLabelTemplate.ROMI: "ROMI",
    FieldLabelTemplate.BUDGET: "Бюджет",
    FieldLabelTemplate.CAPACITY: "Свободные места",
    FieldLabelTemplate.ACTION_STATE: "Состояние действия",
    FieldLabelTemplate.ACTION_COUNT: "Количество действий",
    FieldLabelTemplate.CHECKER_HEALTH: "Проверяющий агент",
    FieldLabelTemplate.HISTORY_STATE: "История",
    FieldLabelTemplate.WINDOW_START: "Начало периода",
    FieldLabelTemplate.WINDOW_END: "Конец периода",
    FieldLabelTemplate.THRESHOLD: "Порог",
    FieldLabelTemplate.DRR: "ДРР",
    FieldLabelTemplate.AGREEMENT: "Согласие",
    FieldLabelTemplate.ACCURACY: "Точность",
    FieldLabelTemplate.PRODUCT_SHARE: "Доля продукта",
}

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def _coverage_issue(message: str) -> CheckIssue:
    return CheckIssue(
        code="REPORT_COVERAGE_INCOMPLETE",
        message=message,
        blocking=True,
    )


def validate_report_coverage(request: ReportCheckRequest) -> CoverageResult:
    """Проверяет точное взаимно-однозначное соответствие полей и утверждений."""

    issues: list[CheckIssue] = []
    payload = request.payload

    if not isinstance(payload.template, ReportTemplate):
        issues.append(_coverage_issue("Шаблон отчёта не входит в закрытый список"))
    if not isinstance(payload.mixed_windows_explicit, bool):
        issues.append(_coverage_issue("Признак смешанных периодов имеет неверный тип"))

    section_ids = [section.section_id for section in payload.sections]
    field_ids = [field.field_id for field in payload.fields]
    claim_ids = [claim.claim_id for claim in request.claims]
    claim_field_ids = [claim.field_id for claim in request.claims]

    if len(section_ids) != len(set(section_ids)):
        issues.append(_coverage_issue("Найдены повторяющиеся разделы"))
    if len(field_ids) != len(set(field_ids)):
        issues.append(_coverage_issue("Найдены повторяющиеся поля"))
    if len(claim_ids) != len(set(claim_ids)):
        issues.append(_coverage_issue("Найдены повторяющиеся claim_id"))
    if any(field_id is None for field_id in claim_field_ids):
        issues.append(_coverage_issue("Утверждение без field_id нельзя вывести"))

    declared_field_ids = [
        field_id for section in payload.sections for field_id in section.field_ids
    ]
    if len(declared_field_ids) != len(set(declared_field_ids)) or set(
        declared_field_ids
    ) != set(field_ids):
        issues.append(_coverage_issue("Каждое поле должно входить ровно в один раздел"))

    fields_by_id = {field.field_id: field for field in payload.fields}
    claims_by_field = {
        claim.field_id: claim for claim in request.claims if claim.field_id is not None
    }
    if len(claims_by_field) != len(request.claims) or set(claims_by_field) != set(
        fields_by_id
    ):
        issues.append(_coverage_issue("Поля и утверждения не образуют точную биекцию"))

    for section in payload.sections:
        if not isinstance(section.template, SectionTemplate):
            issues.append(_coverage_issue("Шаблон раздела не входит в закрытый список"))
        for field_id in section.field_ids:
            field = fields_by_id.get(field_id)
            if field is not None and field.section_id != section.section_id:
                issues.append(
                    _coverage_issue(f"Поле {field_id} привязано к другому разделу")
                )

    for field_id in sorted(set(fields_by_id).intersection(claims_by_field)):
        field = fields_by_id[field_id]
        claim = claims_by_field[field_id]
        if not isinstance(field.label, FieldLabelTemplate):
            issues.append(
                _coverage_issue(f"Поле {field_id} использует произвольный label")
            )
        if not isinstance(field.format, FieldFormat):
            issues.append(
                _coverage_issue(f"Поле {field_id} использует произвольный format")
            )
        if not isinstance(field.category, FactCategory):
            issues.append(
                _coverage_issue(f"Поле {field_id} использует неверную категорию")
            )
        comparable_field = (
            field.field_id,
            field.category,
            field.metric,
            field.value,
            field.subject,
            field.source,
            field.window,
            field.currency,
            field.required,
        )
        comparable_claim = (
            claim.field_id,
            claim.category,
            claim.metric,
            claim.value,
            claim.subject,
            claim.source,
            claim.window,
            claim.currency,
            claim.required,
        )
        if comparable_field != comparable_claim:
            issues.append(_coverage_issue(f"Поле и claim {field_id} расходятся"))

    section_windows = {
        section.window for section in payload.sections if section.window is not None
    }
    field_windows = {
        field.window for field in payload.fields if field.window is not None
    }
    if len(field_windows) > 1 and not payload.mixed_windows_explicit:
        issues.append(_coverage_issue("Смешанные периоды не объявлены явно"))
    if len(field_windows) > 1:
        for field in payload.fields:
            section = next(
                (
                    item
                    for item in payload.sections
                    if item.section_id == field.section_id
                ),
                None,
            )
            if field.window is not None and (
                section is None or section.window != field.window
            ):
                issues.append(
                    _coverage_issue(f"Период поля {field.field_id} не виден в разделе")
                )
    if section_windows and not section_windows.issubset(field_windows):
        issues.append(
            _coverage_issue("Раздел содержит период без соответствующего поля")
        )

    try:
        expected_manifest = report_manifest_sha256(payload, request.claims)
    except (TypeError, ValueError) as exc:
        issues.append(_coverage_issue(f"Манифест отчёта нельзя канонизировать: {exc}"))
        expected_manifest = request.manifest_sha256
    if request.manifest_sha256 != expected_manifest:
        issues.append(_coverage_issue("Хеш отчёта не совпадает с его содержимым"))

    return CoverageResult(
        complete=not issues,
        manifest_sha256=expected_manifest,
        issues=tuple(issues),
    )


def _plain_text(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return _CONTROL_CHARS.sub(" ", text).strip()


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Неконечное число нельзя вывести")
    if value == value.to_integral_value():
        return f"{value:.0f}"
    return format(value.normalize(), "f")


def _format_field_value(value: object, field_format: FieldFormat) -> str:
    if value is None:
        return "нет данных"
    if field_format is FieldFormat.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("INTEGER требует int")
        return str(value)
    if field_format in {FieldFormat.USD, FieldFormat.LCY, FieldFormat.PERCENT}:
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise ValueError(f"{field_format.value} требует число")
        decimal_value = value if isinstance(value, Decimal) else Decimal(value)
        rendered = _decimal_text(decimal_value)
        if field_format is FieldFormat.USD:
            return f"${rendered}"
        if field_format is FieldFormat.LCY:
            return f"{rendered} ¤"
        return f"{rendered}%"
    if field_format in {FieldFormat.TEXT, FieldFormat.STATUS, FieldFormat.DATETIME}:
        if isinstance(value, Enum):
            return _plain_text(value.value)
        return _plain_text(value)
    raise ValueError("Неизвестный формат поля")


def _safe_result(
    result: ReportCheckResult, verdict: ReportVerdict
) -> ReportCheckResult:
    if result.verdict is verdict:
        return result
    return replace(result, verdict=verdict)


def render_safe_notice(result: ReportCheckResult) -> RenderedReport:
    """Строит фиксированное уведомление без спорных чисел, имён и ID."""

    if result.verdict is ReportVerdict.CHECKER_UNAVAILABLE:
        text = "⚠️ Отчёт не отправлен: проверяющий агент недоступен."
    else:
        text = "⛔ Отчёт не отправлен: проверка данных не пройдена."
    return RenderedReport(
        text=text,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        rendered_field_ids=(),
        manifest_sha256=result.manifest_sha256,
        verdict=result.verdict,
        check_id=result.check_id,
    )


def render_checked_report(
    request: ReportCheckRequest,
    result: ReportCheckResult,
) -> RenderedReport:
    """Рендерит только поля, разрешённые результатом независимой проверки."""

    coverage = validate_report_coverage(request)
    if not coverage.complete or result.manifest_sha256 != request.manifest_sha256:
        return render_safe_notice(_safe_result(result, ReportVerdict.BLOCKED))
    if result.verdict in {ReportVerdict.BLOCKED, ReportVerdict.CHECKER_UNAVAILABLE}:
        return render_safe_notice(result)

    claims_by_id = {claim.claim_id: claim for claim in request.claims}
    seen_claim_ids: set[str] = set()
    matched_field_ids: set[str] = set()
    outcome_state_by_field: dict[str, ClaimState] = {}
    for outcome in result.outcomes:
        claim = claims_by_id.get(outcome.claim.claim_id)
        if claim != outcome.claim or outcome.claim.claim_id in seen_claim_ids:
            return render_safe_notice(_safe_result(result, ReportVerdict.BLOCKED))
        seen_claim_ids.add(outcome.claim.claim_id)
        if outcome.claim.field_id is None:
            return render_safe_notice(_safe_result(result, ReportVerdict.BLOCKED))
        outcome_state_by_field[outcome.claim.field_id] = outcome.state
        if outcome.state is ClaimState.MATCH:
            matched_field_ids.add(outcome.claim.field_id)

    all_field_ids = {field.field_id for field in request.payload.fields}
    if seen_claim_ids != set(claims_by_id):
        return render_safe_notice(_safe_result(result, ReportVerdict.BLOCKED))
    if result.verdict is ReportVerdict.VERIFIED:
        expected_field_ids = all_field_ids
        if matched_field_ids != all_field_ids:
            return render_safe_notice(_safe_result(result, ReportVerdict.BLOCKED))
    else:
        expected_field_ids = matched_field_ids
        if not expected_field_ids:
            return render_safe_notice(_safe_result(result, ReportVerdict.BLOCKED))

    fields_by_id = {field.field_id: field for field in request.payload.fields}
    lines = [f"✅ {_REPORT_TITLES[request.payload.template]}"]
    rendered_field_ids: list[str] = []
    try:
        for section in request.payload.sections:
            visible_ids = [
                field_id
                for field_id in section.field_ids
                if field_id in expected_field_ids
            ]
            if not visible_ids:
                continue
            lines.append("")
            lines.append(_SECTION_TITLES[section.template])
            for field_id in visible_ids:
                field = fields_by_id[field_id]
                if outcome_state_by_field.get(field_id) is not ClaimState.MATCH:
                    raise ValueError("Renderer пытается вывести неподтверждённое поле")
                label = _FIELD_LABELS[field.label]
                value = _format_field_value(field.value, field.format)
                lines.append(f"{label}: {value}")
                rendered_field_ids.append(field_id)
    except (KeyError, TypeError, ValueError):
        return render_safe_notice(_safe_result(result, ReportVerdict.BLOCKED))

    if set(rendered_field_ids) != expected_field_ids or len(rendered_field_ids) != len(
        expected_field_ids
    ):
        return render_safe_notice(_safe_result(result, ReportVerdict.BLOCKED))

    text = "\n".join(lines)
    return RenderedReport(
        text=text,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        rendered_field_ids=tuple(rendered_field_ids),
        manifest_sha256=request.manifest_sha256,
        verdict=result.verdict,
        check_id=result.check_id,
    )
