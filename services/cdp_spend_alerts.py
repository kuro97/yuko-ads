"""Алерты расхода по завершённому дню из CDP.

Контур только читает CDP daily-report за вчера и 7 предыдущих дней.
Высокий расход сигнализируется сразу; normal, low и zero требуют два
одинаковых посегментных снимка с интервалом не менее 60 минут.
Никаких FB fallback, пауз объявлений или изменений бюджета здесь нет.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from services import cdp_client, notifications, state_store

logger = logging.getLogger(__name__)

SpendSegment = Literal["total", "online"]
AssessmentStatus = Literal["normal", "low", "no_spend", "high", "unavailable"]
AssessmentReason = Literal[
    "below_low_threshold",
    "confirmed_zero",
    "above_high_threshold",
    "no_baseline",
]
DataReason = Literal[
    "cdp_error",
    "empty_response",
    "invalid_row",
    "missing_date",
    "duplicate_row",
    "city_coverage_mismatch",
    "missing_online",
    "no_baseline",
]
SpendRunStatus = Literal[
    "before_window",
    "already_resolved",
    "awaiting_confirmation",
    "evaluated",
    "degraded",
]

_SEGMENTS: tuple[SpendSegment, ...] = ("total", "online")
_TZ_LOCAL = ZoneInfo("Etc/GMT-5")
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "cdp_spend_alerts_state.json"

_WINDOW_START_HOUR = 12
_CONFIRMATION_MINUTES = 60
_WARNING_COOLDOWN_HOURS = 6
_LOW_RATIO = 0.2
_HIGH_RATIO = 1.5


class CdpDailySpendRow(BaseModel):
    """Runtime-схема используемой части строки CDP."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    report_date: date
    city: str = Field(min_length=1)
    ad_spend: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("city")
    @classmethod
    def validate_city(cls, value: str) -> str:
        """Принимает только точное непустое имя CDP-города."""
        if value != value.strip() or value == "_total":
            raise ValueError("city должен быть непустым точным CDP city, не _total")
        return value

    @field_validator("ad_spend", mode="before")
    @classmethod
    def validate_ad_spend_type(cls, value: object) -> object:
        """Запрещает coercion строк и bool в zero-spend."""
        # bool — подкласс int, поэтому исключаем его отдельно.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("ad_spend должен быть JSON number, не bool/строка")
        return value


@dataclass(frozen=True, slots=True)
class SegmentSnapshot:
    """Каноничный восьмидневный снимок одного сегмента."""

    segment: SpendSegment
    target_date: date
    daily_usd: tuple[tuple[date, float], ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SnapshotValidation:
    """Типизированный успех или reason code валидации снимка."""

    snapshot: SegmentSnapshot | None
    reason_code: DataReason | None


@dataclass(frozen=True, slots=True)
class SpendAssessment:
    """Оценка target-day относительно среднего за предыдущие 7 дней."""

    segment: SpendSegment
    target_date: date
    status: AssessmentStatus
    target_usd: float | None
    baseline_avg_usd: float | None
    ratio: float | None
    reason_code: AssessmentReason | None


class SpendRunResult(TypedDict):
    """Результат never-throw прогона CDP spend-алертов."""

    target_date: str | None
    alerts_sent: int
    alerts_skipped: int
    warnings_sent: int
    segments_resolved: list[SpendSegment]
    ok: bool
    status: SpendRunStatus


def _expected_dates(target_date: date) -> tuple[date, ...]:
    """Возвращает 7 baseline-дат и target по возрастанию."""
    return tuple(target_date - timedelta(days=offset) for offset in range(7, -1, -1))


def _raw_date_in_window(item: object, window_start: date, target_date: date) -> bool:
    """Отсекает только явно внеоконные строки; битые оставляет валидатору."""
    if not isinstance(item, dict):
        return True
    raw_date = item.get("report_date")
    try:
        parsed_date = raw_date if type(raw_date) is date else date.fromisoformat(raw_date)
    except (TypeError, ValueError):
        return True
    return window_start <= parsed_date <= target_date


def _canonical_segment_snapshot(
    items: list[dict],
    segment: SpendSegment,
    target_date: date,
) -> SnapshotValidation:
    """Never-throw валидация и SHA-256 fingerprint отдельного сегмента."""
    try:
        if not items:
            return SnapshotValidation(None, "empty_response")
        if segment not in _SEGMENTS:
            return SnapshotValidation(None, "invalid_row")

        dates = _expected_dates(target_date)
        expected_date_set = set(dates)
        window_start = dates[0]

        if segment == "online":
            # Сначала выбираем exact Online: битые чужие города его не блокируют.
            selected = [
                item
                for item in items
                if isinstance(item, dict)
                and item.get("city") == "Онлайн"
                and _raw_date_in_window(item, window_start, target_date)
            ]
            if not selected:
                return SnapshotValidation(None, "missing_online")
        else:
            selected = [
                item for item in items if _raw_date_in_window(item, window_start, target_date)
            ]
            if not selected:
                return SnapshotValidation(None, "missing_date")

        rows: list[CdpDailySpendRow] = []
        for item in selected:
            try:
                row = CdpDailySpendRow.model_validate(item)
            except (ValidationError, TypeError, ValueError):
                return SnapshotValidation(None, "invalid_row")
            if row.report_date not in expected_date_set:
                continue
            rows.append(row)

        if not rows:
            reason: DataReason = "missing_online" if segment == "online" else "missing_date"
            return SnapshotValidation(None, reason)

        unique_pairs: set[tuple[date, str]] = set()
        for row in rows:
            pair = (row.report_date, row.city)
            if pair in unique_pairs:
                return SnapshotValidation(None, "duplicate_row")
            unique_pairs.add(pair)

        rows_by_date: dict[date, list[CdpDailySpendRow]] = {day: [] for day in dates}
        for row in rows:
            rows_by_date[row.report_date].append(row)

        missing_dates = [day for day, day_rows in rows_by_date.items() if not day_rows]
        if missing_dates:
            reason = "missing_online" if segment == "online" else "missing_date"
            return SnapshotValidation(None, reason)

        if segment == "online":
            if any(len(day_rows) != 1 for day_rows in rows_by_date.values()):
                return SnapshotValidation(None, "duplicate_row")
        else:
            city_sets = [{row.city for row in day_rows} for day_rows in rows_by_date.values()]
            if any(city_set != city_sets[0] for city_set in city_sets[1:]):
                return SnapshotValidation(None, "city_coverage_mismatch")

        canonical_rows = sorted(
            (
                row.report_date.isoformat(),
                row.city,
                0.0 if row.ad_spend == 0 else row.ad_spend,
            )
            for row in rows
        )
        fingerprint_payload = json.dumps(
            canonical_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
        daily_usd = tuple(
            (day, sum(row.ad_spend for row in rows_by_date[day]))
            for day in dates
        )

        return SnapshotValidation(
            SegmentSnapshot(
                segment=segment,
                target_date=target_date,
                daily_usd=daily_usd,
                fingerprint=fingerprint,
            ),
            None,
        )
    except Exception as exc:
        logger.warning(
            "cdp_spend_alerts: непредвиденная ошибка валидации %s: %s",
            segment,
            type(exc).__name__,
        )
        return SnapshotValidation(None, "invalid_row")


def build_assessment(snapshot: SegmentSnapshot) -> SpendAssessment:
    """Считает target/baseline/ratio; zero с ненулевой нормой даёт no_spend."""
    target_usd = snapshot.daily_usd[-1][1]
    baseline_values = [spend for _, spend in snapshot.daily_usd[:-1]]
    baseline_avg_usd = sum(baseline_values) / len(baseline_values)

    if baseline_avg_usd == 0:
        return SpendAssessment(
            segment=snapshot.segment,
            target_date=snapshot.target_date,
            status="unavailable",
            target_usd=target_usd,
            baseline_avg_usd=baseline_avg_usd,
            ratio=None,
            reason_code="no_baseline",
        )

    ratio = target_usd / baseline_avg_usd
    if target_usd == 0:
        status: AssessmentStatus = "no_spend"
        reason_code: AssessmentReason | None = "confirmed_zero"
    elif ratio < _LOW_RATIO:
        status = "low"
        reason_code = "below_low_threshold"
    elif ratio > _HIGH_RATIO:
        status = "high"
        reason_code = "above_high_threshold"
    else:
        status = "normal"
        reason_code = None

    return SpendAssessment(
        segment=snapshot.segment,
        target_date=snapshot.target_date,
        status=status,
        target_usd=target_usd,
        baseline_avg_usd=baseline_avg_usd,
        ratio=ratio,
        reason_code=reason_code,
    )


def _segment_label(segment: SpendSegment | Literal["all"]) -> str:
    """Возвращает безопасную фиксированную Telegram-метку сегмента."""
    return {
        "total": "Итого (включая Онлайн)",
        "online": "Онлайн",
        "all": "все сегменты",
    }[segment]


def _format_usd(value: float | None) -> str:
    """Форматирует USD без ложной точности."""
    if value is None:
        return "н/д"
    return f"${value:,.0f}"


def _format_signal(assessment: SpendAssessment) -> str:
    """Формирует HTML-safe low/high/no_spend сообщение основного бота."""
    segment_label = _segment_label(assessment.segment)
    report_date = assessment.target_date.strftime("%d.%m.%Y")
    baseline = _format_usd(assessment.baseline_avg_usd)
    ratio = assessment.ratio or 0.0

    if assessment.status == "no_spend":
        return (
            "🚨 <b>Расход CDP — нет открутки</b>\n"
            f"Сегмент: {segment_label}\n"
            f"Дата: {report_date} · CDP · FB+Google · завершённый день\n"
            "CDP дважды зафиксировал $0 с интервалом 60+ минут\n"
            f"Среднее 7 дней: {baseline} · темп: {ratio:.2f}× · порог: <{_LOW_RATIO:.2f}×"
        )

    if assessment.status not in ("low", "high"):
        raise ValueError("Сигнал форматируется только для low/high/no_spend")

    title = "ниже нормы" if assessment.status == "low" else "выше нормы"
    threshold = f"<{_LOW_RATIO:.2f}×" if assessment.status == "low" else f">{_HIGH_RATIO:.2f}×"
    return (
        f"⚠️ <b>Расход CDP — {title}</b>\n"
        f"Сегмент: {segment_label}\n"
        f"Дата: {report_date} · CDP · FB+Google · завершённый день\n"
        f"Факт: {_format_usd(assessment.target_usd)} · среднее 7 дней: {baseline}\n"
        f"Темп: {ratio:.2f}× · порог: {threshold}"
    )


_DATA_REASON_TEXT: dict[DataReason, str] = {
    "cdp_error": "CDP daily-report сейчас недоступен",
    "empty_response": "CDP вернул пустой daily-report",
    "invalid_row": "CDP вернул невалидную строку расхода",
    "missing_date": "в daily-report не хватает одной или нескольких дат",
    "duplicate_row": "CDP вернул дублирующиеся строки",
    "city_coverage_mismatch": "состав городов различается между датами",
    "missing_online": "не хватает точной строки «Онлайн» за одну или несколько дат",
    "no_baseline": "средний расход за 7 baseline-дней равен $0",
}


def _format_data_warning(
    segment: SpendSegment | Literal["all"],
    target_date: date,
    reason_code: DataReason,
) -> str:
    """Формирует warning по reason code без raw exception или API key."""
    return (
        "⚠️ <b>Расход CDP — данные неполные</b>\n"
        f"Сегмент: {_segment_label(segment)}\n"
        f"Дата: {target_date.strftime('%d.%m.%Y')} · CDP · FB+Google · завершённый день\n"
        f"Причина: {_DATA_REASON_TEXT[reason_code]}.\n"
        "Расход не оцениваю; FB fallback отключён; повторю через час."
    )


def _normalize_moment(now: datetime | None) -> datetime:
    """Приводит now к Etc/GMT-5; naive значение трактует как UTC."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(_TZ_LOCAL)


def _normalize_state(raw_state: dict) -> dict:
    """Восстанавливает ожидаемые state-секции после битого или legacy JSON."""
    state = dict(raw_state) if isinstance(raw_state, dict) else {}
    for section_name in ("candidate", "resolved", "sent"):
        if not isinstance(state.get(section_name), dict):
            state[section_name] = {}
    return state


def _resolved_segments(state: dict, target_date: date) -> list[SpendSegment]:
    """Возвращает сегменты, resolved именно на target-date."""
    resolved = state["resolved"]
    target_key = target_date.isoformat()
    return [segment for segment in _SEGMENTS if resolved.get(segment) == target_key]


def _parse_state_datetime(value: object) -> datetime | None:
    """Безопасно парсит state timestamp; legacy naive трактует как локальное время."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_TZ_LOCAL)
    return parsed.astimezone(_TZ_LOCAL)


def _send_main_bot(text: str) -> bool:
    """Отправляет в основной бот и никогда не бросает ошибку наружу."""
    try:
        return bool(notifications.send_telegram(text, channel="ads"))
    except Exception as exc:
        logger.warning("cdp_spend_alerts: Telegram exception: %s", type(exc).__name__)
        return False


def _warning_is_deduped(key: str, state: dict, now: datetime) -> bool:
    """Проверяет шестичасовой cooldown data-gap warning."""
    last_sent = _parse_state_datetime(state["sent"].get(key))
    if last_sent is None:
        return False
    return (now - last_sent).total_seconds() < _WARNING_COOLDOWN_HOURS * 3600


def _deliver_warning(
    segment: SpendSegment | Literal["all"],
    target_date: date,
    reason_code: DataReason,
    state: dict,
    now: datetime,
) -> tuple[int, int, bool]:
    """Отправляет deduped data-gap warning: (warnings_sent, skipped, state_changed)."""
    key_segment = segment if segment != "all" else "all"
    key = f"spend_data:{key_segment}:{target_date.isoformat()}"
    if _warning_is_deduped(key, state, now):
        return 0, 1, False
    if not _send_main_bot(_format_data_warning(segment, target_date, reason_code)):
        return 0, 1, False
    state["sent"][key] = now.isoformat()
    return 1, 0, True


def _candidate_is_confirmed(
    candidate: object,
    snapshot: SegmentSnapshot,
    now: datetime,
) -> bool:
    """Проверяет target, fingerprint и интервал 60+ минут."""
    if not isinstance(candidate, dict):
        return False
    if candidate.get("target_date") != snapshot.target_date.isoformat():
        return False
    if candidate.get("fingerprint") != snapshot.fingerprint:
        return False
    observed_at = _parse_state_datetime(candidate.get("observed_at"))
    if observed_at is None:
        return False
    return (now - observed_at).total_seconds() >= _CONFIRMATION_MINUTES * 60


def _candidate_matches_snapshot(candidate: object, snapshot: SegmentSnapshot) -> bool:
    """Проверяет тот же target/fingerprint без time-gate."""
    return (
        isinstance(candidate, dict)
        and candidate.get("target_date") == snapshot.target_date.isoformat()
        and candidate.get("fingerprint") == snapshot.fingerprint
        and _parse_state_datetime(candidate.get("observed_at")) is not None
    )


def _set_candidate(state: dict, snapshot: SegmentSnapshot, now: datetime) -> None:
    """Записывает новый per-segment candidate."""
    state["candidate"][snapshot.segment] = {
        "target_date": snapshot.target_date.isoformat(),
        "fingerprint": snapshot.fingerprint,
        "observed_at": now.isoformat(),
    }


def _clear_candidate(state: dict, segment: SpendSegment) -> bool:
    """Удаляет candidate только указанного сегмента."""
    if segment not in state["candidate"]:
        return False
    state["candidate"].pop(segment, None)
    return True


def _resolve_segment(state: dict, segment: SpendSegment, target_date: date) -> bool:
    """Помечает сегмент resolved и удаляет его candidate."""
    target_key = target_date.isoformat()
    changed = state["resolved"].get(segment) != target_key
    state["resolved"][segment] = target_key
    return _clear_candidate(state, segment) or changed


def _deliver_signal(
    assessment: SpendAssessment,
    state: dict,
    now: datetime,
) -> Literal["sent", "deduped", "failed"]:
    """Отправляет сигнал с постоянным dedup-ключом segment/date."""
    key = f"spend_signal:{assessment.segment}:{assessment.target_date.isoformat()}"
    if state["sent"].get(key):
        return "deduped"
    if not _send_main_bot(_format_signal(assessment)):
        return "failed"
    state["sent"][key] = now.isoformat()
    return "sent"


def _save_state(state: dict) -> bool:
    """Сохраняет state атомарно и превращает ошибку в never-throw False."""
    try:
        state_store.save_json_state(_STATE_FILE, state)
        return True
    except Exception as exc:
        logger.error("cdp_spend_alerts: не удалось сохранить state: %s", type(exc).__name__)
        return False


def _base_result(target_date: date, status: SpendRunStatus) -> SpendRunResult:
    """Создаёт типизированный нулевой результат для target-date."""
    return {
        "target_date": target_date.isoformat(),
        "alerts_sent": 0,
        "alerts_skipped": 0,
        "warnings_sent": 0,
        "segments_resolved": [],
        "ok": True,
        "status": status,
    }


def run_cdp_spend_alerts(now: datetime | None = None) -> SpendRunResult:
    """Never-throw orchestration: time gate, CDP GET, stability, Telegram и state."""
    target_date: date | None = None
    try:
        moment = _normalize_moment(now)
        target_date = moment.date() - timedelta(days=1)

        if moment.hour < _WINDOW_START_HOUR:
            return _base_result(target_date, "before_window")

        state = _normalize_state(state_store.load_json_state(_STATE_FILE))
        resolved_before = _resolved_segments(state, target_date)
        if len(resolved_before) == len(_SEGMENTS):
            result = _base_result(target_date, "already_resolved")
            result["segments_resolved"] = resolved_before
            return result

        try:
            items = cdp_client.get_daily_report(target_date - timedelta(days=7), target_date)
        except Exception as exc:
            # Ошибка transport не является новым снимком: candidates не сбрасываем.
            logger.warning("cdp_spend_alerts: CDP GET недоступен: %s", type(exc).__name__)
            warnings_sent, alerts_skipped, state_changed = _deliver_warning(
                "all", target_date, "cdp_error", state, moment
            )
            if state_changed:
                _save_state(state)
            result = _base_result(target_date, "degraded")
            result["warnings_sent"] = warnings_sent
            result["alerts_skipped"] = alerts_skipped
            result["segments_resolved"] = resolved_before
            result["ok"] = False
            return result

        validations: dict[SpendSegment, SnapshotValidation] = {}
        for segment in _SEGMENTS:
            if segment in resolved_before:
                continue
            if not isinstance(items, list):
                validations[segment] = SnapshotValidation(None, "invalid_row")
            else:
                validations[segment] = _canonical_segment_snapshot(items, segment, target_date)

        alerts_sent = 0
        alerts_skipped = 0
        warnings_sent = 0
        state_changed = False
        has_data_gap = False
        has_signal = False
        has_evaluation = False

        for segment in _SEGMENTS:
            if segment in resolved_before:
                continue

            validation = validations[segment]
            if validation.snapshot is None:
                reason_code = validation.reason_code or "invalid_row"
                has_data_gap = True
                state_changed = _clear_candidate(state, segment) or state_changed
                warning_sent, warning_skipped, warning_changed = _deliver_warning(
                    segment,
                    target_date,
                    reason_code,
                    state,
                    moment,
                )
                warnings_sent += warning_sent
                alerts_skipped += warning_skipped
                state_changed = warning_changed or state_changed
                logger.warning(
                    "cdp_spend_alerts: segment=%s unavailable reason=%s",
                    segment,
                    reason_code,
                )
                continue

            snapshot = validation.snapshot
            assessment = build_assessment(snapshot)
            if assessment.status == "unavailable":
                has_data_gap = True
                state_changed = _clear_candidate(state, segment) or state_changed
                warning_sent, warning_skipped, warning_changed = _deliver_warning(
                    segment,
                    target_date,
                    "no_baseline",
                    state,
                    moment,
                )
                warnings_sent += warning_sent
                alerts_skipped += warning_skipped
                state_changed = warning_changed or state_changed
                continue

            if assessment.status == "high":
                # High не ждёт confirmation: полный уже высокий target достаточен.
                has_signal = True
                has_evaluation = True
                state_changed = _clear_candidate(state, segment) or state_changed
                delivery = _deliver_signal(assessment, state, moment)
                if delivery == "sent":
                    alerts_sent += 1
                    state_changed = True
                    state_changed = _resolve_segment(state, segment, target_date) or state_changed
                elif delivery == "deduped":
                    alerts_skipped += 1
                    state_changed = _resolve_segment(state, segment, target_date) or state_changed
                else:
                    alerts_skipped += 1
                continue

            candidate = state["candidate"].get(segment)
            if not _candidate_matches_snapshot(candidate, snapshot):
                _set_candidate(state, snapshot, moment)
                state_changed = True
                continue
            if not _candidate_is_confirmed(candidate, snapshot, moment):
                continue

            has_evaluation = True
            if assessment.status == "normal":
                state_changed = _resolve_segment(state, segment, target_date) or state_changed
                continue

            has_signal = True
            delivery = _deliver_signal(assessment, state, moment)
            if delivery == "sent":
                alerts_sent += 1
                state_changed = True
                state_changed = _resolve_segment(state, segment, target_date) or state_changed
            elif delivery == "deduped":
                alerts_skipped += 1
                state_changed = _resolve_segment(state, segment, target_date) or state_changed
            else:
                alerts_skipped += 1

        if has_data_gap:
            status: SpendRunStatus = "degraded"
        elif has_evaluation:
            status = "evaluated"
        else:
            status = "awaiting_confirmation"

        if state_changed:
            _save_state(state)

        result = _base_result(target_date, status)
        result["alerts_sent"] = alerts_sent
        result["alerts_skipped"] = alerts_skipped
        result["warnings_sent"] = warnings_sent
        result["segments_resolved"] = _resolved_segments(state, target_date)
        result["ok"] = not has_data_gap and not has_signal
        return result
    except Exception as exc:
        logger.error("cdp_spend_alerts: непредвиденная ошибка run: %s", type(exc).__name__)
        if target_date is None:
            return {
                "target_date": None,
                "alerts_sent": 0,
                "alerts_skipped": 0,
                "warnings_sent": 0,
                "segments_resolved": [],
                "ok": False,
                "status": "degraded",
            }
        result = _base_result(target_date, "degraded")
        result["ok"] = False
        return result
