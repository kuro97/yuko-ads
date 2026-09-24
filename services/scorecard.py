"""
Еженедельное табло точности автопилота.

Считает:
- согласие владельца: доля APPROVE от решённых owner_action_decisions
  (кнопок 👍/👎 больше нет; autopilot_feedback остался fallback'ом для истории)
- объём решений автопилота (PAUSED + scale) за период
- ретроспективную точность масштаба: отскейленные → всё ещё победители?

Отправляет табло в Telegram.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

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

# Локальное время (UTC+5 по умолчанию, настраивается)
_TZ_LOCAL = timezone(timedelta(hours=5))

# State-файл для дедупликации еженедельного крона
_SCORECARD_STATE_FILE = Path(__file__).parent.parent / "data" / "scorecard_state.json"

# Порог победителя (совпадает с budget_scaler)
_MIN_QUAL_PCT = 15.0
_MIN_ROMI = 0.0

# Минимум оценок — при меньшем выводим предупреждение
_MIN_FEEDBACK_FOR_TRUST = 3


def _load_scorecard_state() -> dict:
    """Загружает state дедупликации из файла."""
    if not _SCORECARD_STATE_FILE.exists():
        return {}
    try:
        return json.loads(_SCORECARD_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("scorecard: не удалось прочитать state — %s", exc)
        return {}


def _save_scorecard_state(state: dict) -> None:
    """Сохраняет state дедупликации."""
    try:
        _SCORECARD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SCORECARD_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_SCORECARD_STATE_FILE)
    except Exception as exc:
        logger.warning("scorecard: не удалось сохранить state — %s", exc)


def _get_scale_ad_ids(since_iso: str) -> list[str]:
    """Возвращает ad_id из решений о масштабировании за период.

    Ищет в decisions.db записи где action = 'SCALED' или action LIKE '%scale%'
    и confirmed_by LIKE 'autopilot%'.
    """
    try:
        from agent.database import DB_PATH
        if DB_PATH is None:
            return []
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT ad_id FROM decisions
                WHERE (action = 'SCALED' OR action LIKE '%scale%' OR action LIKE '%SCALE%')
                  AND confirmed_by LIKE 'autopilot%'
                  AND created_at >= ?
                """,
                (since_iso,),
            ).fetchall()
            return [r["ad_id"] for r in rows]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("scorecard: не удалось получить scale-решения — %s", exc)
        return []


def _get_decision_counts(since_iso: str) -> dict[str, int]:
    """Считает PAUSED и scale решения автопилота за период."""
    try:
        from agent.database import DB_PATH
        if DB_PATH is None:
            return {"paused": 0, "scaled": 0}
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            # Паузы
            paused_row = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM decisions
                WHERE action = 'PAUSED'
                  AND confirmed_by LIKE 'autopilot%'
                  AND created_at >= ?
                """,
                (since_iso,),
            ).fetchone()
            # Масштабирования
            scaled_row = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM decisions
                WHERE (action = 'SCALED' OR action LIKE '%scale%' OR action LIKE '%SCALE%')
                  AND confirmed_by LIKE 'autopilot%'
                  AND created_at >= ?
                """,
                (since_iso,),
            ).fetchone()
            return {
                "paused": paused_row["cnt"] if paused_row else 0,
                "scaled": scaled_row["cnt"] if scaled_row else 0,
            }
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("scorecard: не удалось получить счётчики решений — %s", exc)
        return {"paused": 0, "scaled": 0}


def _check_scale_accuracy(scaled_ad_ids: list[str]) -> dict:
    """Проверяет ретроспективную точность масштаба.

    Для каждого отскейленного ad_id смотрит текущие qual_pct / romi в creative_kb.
    Победитель = qual_pct >= 15 И romi > 0.

    Returns:
        {
            "total": int,       # сколько отскейленных проверено
            "winners": int,     # сколько всё ещё победители
            "accuracy_pct": float | None,  # winners/total * 100
        }
    """
    if not scaled_ad_ids:
        return {"total": 0, "winners": 0, "accuracy_pct": None}

    try:
        from services.creative_intelligence import _get_connection as _kb_conn
        conn = _kb_conn()
        try:
            # Строим параметризованный запрос с нужным числом плейсхолдеров
            placeholders = ",".join("?" * len(scaled_ad_ids))
            rows = conn.execute(
                f"SELECT ad_id, qual_pct, romi FROM creative_kb WHERE ad_id IN ({placeholders})",
                scaled_ad_ids,
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("scorecard: не удалось прочитать creative_kb для scale-точности — %s", exc)
        return {"total": len(scaled_ad_ids), "winners": 0, "accuracy_pct": None}

    # Объявления которых нет в KB — считаем неизвестными (не победители)
    total_checked = len(scaled_ad_ids)

    winners = 0
    for row in rows:
        qual = row["qual_pct"]
        romi = row["romi"]
        if (
            qual is not None and qual >= _MIN_QUAL_PCT
            and romi is not None and romi > _MIN_ROMI
        ):
            winners += 1

    accuracy_pct: float | None = None
    if total_checked > 0:
        accuracy_pct = round(winners / total_checked * 100, 1)

    return {
        "total": total_checked,
        "winners": winners,
        "accuracy_pct": accuracy_pct,
    }


def build_scorecard(days: int = 7, *, now: datetime | None = None) -> dict:
    """Собирает данные для еженедельного табло автопилота.

    Args:
        days: период в днях (по умолчанию 7)

    Returns:
        {
            "period_days": int,
            "feedback": {"up": int, "down": int, "total": int, "agreement_pct": float | None},
            "decisions": {"paused": int, "scaled": int},
            "scale_accuracy": {"total": int, "winners": int, "accuracy_pct": float | None},
        }
    """
    now = now or datetime.now(_TZ_LOCAL)
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=_TZ_LOCAL)
    since = now - timedelta(days=days)
    since_iso = since.strftime("%Y-%m-%d %H:%M:%S")

    # 1. Согласие владельца
    from services.autopilot_feedback import get_feedback_stats
    feedback = get_feedback_stats(since_days=days)

    # 2. Объём решений
    decisions = _get_decision_counts(since_iso)

    # 3. Ретроспективная точность масштаба
    scaled_ids = _get_scale_ad_ids(since_iso)
    scale_accuracy = _check_scale_accuracy(scaled_ids)

    return {
        "period_days": days,
        "feedback": feedback,
        "decisions": decisions,
        "scale_accuracy": scale_accuracy,
    }


def format_scorecard(data: dict) -> str:
    """Форматирует табло в HTML для Telegram (≤ 20 строк).

    Args:
        data: результат build_scorecard()

    Returns:
        Строка HTML для legacy preview; production delivery использует typed renderer.
    """
    period = data.get("period_days", 7)
    fb = data.get("feedback", {})
    dec = data.get("decisions", {})
    sa = data.get("scale_accuracy", {})

    lines = [f"📊 <b>Табло автопилота за {period} дней</b>"]
    lines.append("")

    # Согласие
    total_fb = fb.get("total", 0)
    if total_fb > 0:
        agree = fb.get("agreement_pct")
        agree_str = f"{agree}%" if agree is not None else "—"
        lines.append(
            f"👍 Согласие: <b>{agree_str}</b> "
            f"({fb.get('up', 0)} 👍 / {fb.get('down', 0)} 👎, всего {total_fb})"
        )
    else:
        lines.append("👍 Согласие: нет оценок")

    # Объём решений
    paused = dec.get("paused", 0)
    scaled = dec.get("scaled", 0)
    lines.append(f"🤖 Решений: <b>{paused}</b> пауз / <b>{scaled}</b> поднятий")

    # Точность масштаба (ретро)
    sa_total = sa.get("total", 0)
    if sa_total > 0:
        sa_acc = sa.get("accuracy_pct")
        sa_str = f"{sa_acc}%" if sa_acc is not None else "—"
        sa_winners = sa.get("winners", 0)
        lines.append(
            f"📈 Масштаб точен: <b>{sa_str}</b> "
            f"({sa_winners} из {sa_total} отскейленных всё ещё победители)"
        )
    else:
        lines.append("📈 Масштаб: решений о повышении за период нет")

    # Призыв к оценке если мало фидбэка
    if total_fb < _MIN_FEEDBACK_FOR_TRUST:
        lines.append("")
        lines.append(
            "⚠️ <i>Мало 👍/👎 — жми кнопки под решениями, "
            "чтобы табло было точнее</i>"
        )

    return "\n".join(lines)


def _required_count(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name}: ожидается целое неотрицательное число")
    return value


def _required_decimal(value: object, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name}: обязательное число отсутствует")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name}: некорректное число") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name}: число должно быть конечным")
    return parsed


def _scorecard_field_claim(
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


def build_scorecard_request(
    data: dict,
    *,
    generated_at: datetime,
) -> ReportCheckRequest:
    """Типизирует каждое число scorecard; отсутствие не становится нулём."""

    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at должен содержать timezone")
    days = _required_count(data.get("period_days"), "period_days")
    if days <= 0:
        raise ValueError("period_days должен быть положительным")
    window = TimeWindow(
        start=generated_at - timedelta(days=days),
        end=generated_at,
        timezone_name="Etc/GMT-5",
        semantic="autopilot_scorecard_exact_window",
    )
    feedback = data.get("feedback")
    decisions = data.get("decisions")
    accuracy = data.get("scale_accuracy")
    if not isinstance(feedback, dict) or not isinstance(decisions, dict) or not isinstance(accuracy, dict):
        raise ValueError("scorecard snapshot неполон")

    fields: list[ReportField] = []
    claims: list[FactClaim] = []

    def add(**kwargs: object) -> None:
        field, claim = _scorecard_field_claim(**kwargs)  # type: ignore[arg-type]
        fields.append(field)
        claims.append(claim)

    up = _required_count(feedback.get("up"), "feedback.up")
    down = _required_count(feedback.get("down"), "feedback.down")
    total = _required_count(feedback.get("total"), "feedback.total")
    if total != up + down:
        raise ValueError("feedback.total не равен up+down")
    for name, value in (("up", up), ("down", down), ("total", total)):
        add(
            field_id=f"scorecard.feedback.{name}",
            section_id="feedback",
            label=FieldLabelTemplate.MATCH if name != "total" else FieldLabelTemplate.ACTION_COUNT,
            field_format=FieldFormat.INTEGER,
            subject=SubjectRef(SubjectKind.FEEDBACK, name),
            metric=Metric.FEEDBACK_COUNT,
            value=value,
            source=SourceSystem.AUTOPILOT_FEEDBACK,
            window=window,
        )
    agreement = feedback.get("agreement_pct")
    if agreement is not None:
        agreement_decimal = _required_decimal(agreement, "feedback.agreement_pct")
        expected = (Decimal(up) * Decimal("100") / Decimal(total)) if total else None
        if expected is None or abs(agreement_decimal - expected) > Decimal("0.2"):
            raise ValueError("agreement_pct не совпадает с up/(up+down)")
        add(
            field_id="scorecard.feedback.agreement",
            section_id="feedback",
            label=FieldLabelTemplate.AGREEMENT,
            field_format=FieldFormat.PERCENT,
            subject=SubjectRef(SubjectKind.FEEDBACK, "agreement"),
            metric=Metric.AGREEMENT_PCT,
            value=agreement_decimal,
            source=SourceSystem.AUTOPILOT_FEEDBACK,
            window=window,
        )

    for name in ("paused", "scaled"):
        add(
            field_id=f"scorecard.decisions.{name}",
            section_id="decisions",
            label=FieldLabelTemplate.ACTION_COUNT,
            field_format=FieldFormat.INTEGER,
            subject=SubjectRef(SubjectKind.DECISION, name),
            metric=Metric.DECISION_COUNT,
            value=_required_count(decisions.get(name), f"decisions.{name}"),
            source=SourceSystem.DECISIONS_DB,
            window=window,
        )

    accuracy_total = _required_count(accuracy.get("total"), "scale_accuracy.total")
    accuracy_winners = _required_count(accuracy.get("winners"), "scale_accuracy.winners")
    if accuracy_winners > accuracy_total:
        raise ValueError("scale_accuracy.winners больше total")
    for name, value in (("total", accuracy_total), ("winners", accuracy_winners)):
        add(
            field_id=f"scorecard.accuracy.{name}",
            section_id="accuracy",
            label=FieldLabelTemplate.ACTION_COUNT,
            field_format=FieldFormat.INTEGER,
            subject=SubjectRef(SubjectKind.CREATIVE, f"scale-{name}"),
            metric=Metric.DECISION_COUNT,
            value=value,
            source=SourceSystem.CREATIVE_KB,
            window=window,
        )
    accuracy_pct = accuracy.get("accuracy_pct")
    if accuracy_pct is not None:
        accuracy_decimal = _required_decimal(accuracy_pct, "scale_accuracy.accuracy_pct")
        if accuracy_total == 0:
            raise ValueError("accuracy_pct задан при нулевом total")
        expected = Decimal(accuracy_winners) * Decimal("100") / Decimal(accuracy_total)
        if abs(accuracy_decimal - expected) > Decimal("0.2"):
            raise ValueError("accuracy_pct не совпадает с winners/total")
        add(
            field_id="scorecard.accuracy.pct",
            section_id="accuracy",
            label=FieldLabelTemplate.ACCURACY,
            field_format=FieldFormat.PERCENT,
            subject=SubjectRef(SubjectKind.CREATIVE, "scale-accuracy"),
            metric=Metric.ACCURACY_PCT,
            value=accuracy_decimal,
            source=SourceSystem.CREATIVE_KB,
            window=window,
        )

    sections = tuple(
        ReportSection(
            section_id=section_id,
            template=template,
            field_ids=tuple(field.field_id for field in fields if field.section_id == section_id),
            window=window,
        )
        for section_id, template in (
            ("feedback", SectionTemplate.SUMMARY),
            ("decisions", SectionTemplate.ACTIONS),
            ("accuracy", SectionTemplate.OUTCOMES),
        )
    )
    payload = TypedReportPayload(
        template=ReportTemplate.SCORECARD,
        sections=sections,
        fields=tuple(fields),
        generated_at=generated_at,
        mixed_windows_explicit=False,
    )
    claim_tuple = tuple(claims)
    return ReportCheckRequest(
        correlation_id=f"scorecard:{uuid.uuid4()}",
        payload=payload,
        claims=claim_tuple,
        manifest_sha256=report_manifest_sha256(payload, claim_tuple),
    )


def send_scorecard() -> None:
    """Строит и отправляет табло в Telegram (канал ads)."""
    try:
        generated_at = datetime.now(_TZ_LOCAL)
        data = build_scorecard(days=7, now=generated_at)
        request = build_scorecard_request(data, generated_at=generated_at)
        result = check_report(request, now=generated_at)
        rendered = render_checked_report(request, result)
        delivery = send_checked_report(rendered, channel="ads")
        if not delivery.sent:
            raise RuntimeError("checked Telegram delivery failed")
        logger.info("scorecard: проверенное табло отправлено")
    except Exception as exc:
        logger.error("scorecard: не удалось отправить табло — %s", exc)
        try:
            send_fact_free(
                FactFreeTemplate.CHECKER_INTERNAL_ERROR,
                channel="ads",
                error_type=type(exc).__name__,
            )
        except Exception as delivery_exc:
            logger.error("scorecard: safe notice не отправлен — %s", type(delivery_exc).__name__)
        raise


def should_send_scorecard_this_week(now: datetime | None = None) -> bool:
    """Проверяет, нужно ли отправлять табло в этом тике.

    Условия: воскресенье (weekday==6) И локальный час == 19 И эта неделя ещё не была отправлена.

    Returns:
        True если нужно отправить, False иначе.
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL)

    # Воскресенье, 19:xx по локальному времени
    if now.weekday() != 6 or now.hour != 19:
        return False

    # Дедупликация по ISO-неделе
    iso_week = now.strftime("%G-W%V")  # напр. "2026-W26"
    state = _load_scorecard_state()
    if state.get("last_sent_week") == iso_week:
        return False

    return True


def mark_scorecard_sent(now: datetime | None = None) -> None:
    """Помечает текущую неделю как отправленную."""
    if now is None:
        now = datetime.now(_TZ_LOCAL)
    iso_week = now.strftime("%G-W%V")
    _save_scorecard_state({"last_sent_week": iso_week})
