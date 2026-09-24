"""
Вечерний отчёт автопилота — формат v2.

Переделка по фидбеку владельца: одних расходов мало, нужны квалы, ROMI и
CPL по паузам; заодно вскрылась ошибка окон — кабинет FB
живёт по времени Америки (LA, UTC-8/-7), его «день» в insights API
начинается в 12:00 по локальному времени, а старый отчёт в 21:00 смешивал расход за
~9ч дня кабинета с лидами за локальные сутки → фейковый заниженный CPL.

v2 — честные окна:
- «Сегодня» — расход И лиды строго за окно ДНЯ КАБИНЕТА (с 12:00 по локальному времени),
  дозагружается, финал будет в утреннем дайджесте.
- «Вчера» — расход И лиды за ПОЛНЫЙ закрытый день (расход — закрытый день
  кабинета по _get_fb_spend_for_day, лиды — локальные сутки 00:00-23:59).
- Никаких CPL из смешанных окон.

Собирает сводку за день: вердикт с дельтами, деньги (2 честных окна), что
сделал бот (паузы/удержания ПОЛНЫМИ блоками — переиспользуем
autopilot._format_pause_block/_format_held_section), план на завтра.
Отправляет в Telegram в 21:00 по локальному времени с inline-кнопками (если доступен
telegram_bot).

Формат — блочный, как _format_pause_report в services/autopilot.py:
секции с пустыми строками между собой, жирные заголовки Telegram HTML,
цифры округлены, нулевые пункты не показываются («0 чисток» не пишем).
"""

import html
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from services.formatting import fmt_money
from services.approval_checker_models import (
    ActionResult,
    FactCategory,
    FactClaim,
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

logger = logging.getLogger(__name__)

# Локальный часовой пояс (UTC+5 по умолчанию, настраивается здесь)
_TZ_LOCAL = timezone(timedelta(hours=5))

# Час начала «дня кабинета» FB по локальному времени (кабинет живёт по LA; разница
# LA и локального пояса летом/зимой в проекте одинаково считается как 12ч —
# «день кабинета начинается в 12:00 по локальному времени»).
_FB_ACCOUNT_DAY_START_HOUR = 12

# Путь к state-файлу автопилота
_STATE_PATH = Path(__file__).parent.parent / "data" / "autopilot_state.json"

# Пути к state-файлам, которые читаем напрямую (запуски и карточки ТЗ туда
# не пишутся в decisions_repo — см. тот же паттерн в morning_digest.py)
_LAUNCH_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "auto_launch_state.json"
)
_BRIEF_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "brief_gen_state.json"
)

# Подтверждённые источники автопилота (как в morning_digest._AUTOPILOT_CONFIRMED_BY —
# плюс cleaner/budget_pilot*, их решения тоже относятся к «что сделал бот»)
_AUTOPILOT_CONFIRMED_BY = {
    "autopilot",
    "autopilot_dry",
    "owner_telegram",
    "cleaner",
    "budget_pilot",
    "budget_pilot_dry",
}

# Максимум кнопок «Вернуть» в отчёте
_MAX_UNDO_BUTTONS = 5

# Лимит Telegram на одно сообщение
_TELEGRAM_MAX_LEN = 4096

# Топ-N подъёмов бюджета, показываем кратко в секции «Что сделал бот»
# (паузы теперь ПОЛНЫМИ блоками — см. _section_bot_actions, но подъёмы
# остаются кратким списком, пункт 4 фидбека владельца: «Подъёмы... как было»)
_TOP_RAISED_SHOWN = 3


def _as_decimal(value: object) -> Decimal | None:
    """Нормализует внешнее число без binary-float в typed contract."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _account_subject() -> SubjectRef:
    """Возвращает exact account identity или fail-closed placeholder."""
    try:
        from services.fb_token_provider import get_fb_account_id

        account_id = str(get_fb_account_id()).removeprefix("act_").strip()
        if not account_id or account_id.lower() == "none":
            raise ValueError("FB account id пуст")
    except Exception as exc:
        logger.warning("evening_report: account id недоступен — %s", type(exc).__name__)
        account_id = "unavailable"
    return SubjectRef(SubjectKind.ACCOUNT, account_id)


def _append_fact(
    fields: list[ReportField],
    claims: list[FactClaim],
    section_fields: dict[str, list[str]],
    *,
    field_id: str,
    section_id: str,
    category: FactCategory,
    label: FieldLabelTemplate,
    field_format: FieldFormat,
    subject: SubjectRef,
    metric: Metric,
    value: int | Decimal | str | None,
    source: SourceSystem,
    window: TimeWindow | None,
    currency: str | None = None,
    required: bool = True,
) -> None:
    """Создаёт поле и его единственный идентичный FactClaim вместе.

    Отсутствующее значение (value=None) НИКОГДА не бывает required: требовать
    подтверждения у несуществующего числа бессмысленно, а blocking-претензия
    по нему снесла бы весь отчёт вместо того, чтобы честно скрыть одно поле.
    Чекер при этом не ослабляется: неподтверждённое поле по-прежнему не
    рендерится, вердикт опускается до VERIFIED_WITH_LIMITATIONS.
    """
    required = required and value is not None
    field = ReportField(
        field_id=field_id,
        section_id=section_id,
        category=category,
        label=label,
        format=field_format,
        subject=subject,
        metric=metric,
        value=value,
        source=source,
        window=window,
        currency=currency,
        required=required,
    )
    fields.append(field)
    claims.append(
        FactClaim(
            claim_id=f"claim:{field_id}",
            field_id=field_id,
            category=category,
            subject=subject,
            metric=metric,
            value=value,
            source=source,
            window=window,
            currency=currency,
            required=required,
        )
    )
    section_fields[section_id].append(field_id)


def _fb_account_day_windows(now: datetime) -> tuple[TimeWindow, TimeWindow]:
    """Сутки кабинета FB для «сегодня» и «вчера» — ровно то, что меряет
    morning_digest._get_fb_spend_for_day(ISO-дата).

    День кабинета = [12:00 по локальному времени, 12:00 по локальному времени следующего дня): insights
    time_range since=until=дата считается по времени аккаунта (LA), а полночь
    кабинета — это 12:00 по локальному времени (_FB_ACCOUNT_DAY_START_HOUR). Обе границы попадают ровно в полночь
    кабинета, поэтому Graph способен доказать такое окно точно, без
    округления частичного интервала.

    «Сегодня» — тот же самый день кабинета, но ещё ОТКРЫТЫЙ: FB отдаёт по
    нему расход, накопленный на текущий момент, а не финальный. Окно
    подписано семантикой fb_account_open_day и футером отчёта «расход FB
    дозагружается» — период честно назван сутками кабинета, а не «12:00 →
    сейчас», иначе claim подписывал бы дневное число частичным интервалом.
    """
    # Границы строим строго по тем же ISO-датам, которые _compute_money_windows
    # передаёт в _get_fb_spend_for_day: now.date() и (now-1д).date().
    today_start = now.replace(
        hour=_FB_ACCOUNT_DAY_START_HOUR, minute=0, second=0, microsecond=0
    )
    open_day = TimeWindow(
        start=today_start,
        end=today_start + timedelta(days=1),
        timezone_name="Etc/GMT-5",
        semantic="fb_account_open_day",
    )
    closed_day = TimeWindow(
        start=today_start - timedelta(days=1),
        end=today_start,
        timezone_name="Etc/GMT-5",
        semantic="fb_account_closed_day",
    )
    return open_day, closed_day


def _history_snapshot(window: TimeWindow) -> tuple[int | None, str]:
    """Считает только CONFIRMED из WAL; незавершённость не становится успехом."""
    try:
        from services.approval_audit import read_action_history

        history = read_action_history(window)
    except Exception as exc:
        logger.warning(
            "evening_report: approval history недоступна — %s", type(exc).__name__
        )
        return None, "UNAVAILABLE"

    confirmed = sum(item.result is ActionResult.CONFIRMED for item in history)
    has_gap = any(
        item.result is not ActionResult.CONFIRMED
        or item.reconciliation_required
        or item.completed_at is None
        for item in history
    )
    return confirmed, "PENDING" if has_gap else "CLEAR"


def build_evening_report_request(
    now: datetime,
    windows: dict,
) -> ReportCheckRequest:
    """Строит закрытый typed-манифест вечернего отчёта.

    Google и свободный текст старого отчёта намеренно не попадают в этот
    манифест: для них пока нет независимого SourceSystem. Они не будут
    отправлены как проверенные факты.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_LOCAL)
    else:
        now = now.astimezone(_TZ_LOCAL)
    account = _account_subject()
    checker = SubjectRef(SubjectKind.CHECKER, "approval-action-history")
    today_from, today_to = _today_window_bounds(now)
    yesterday_from, yesterday_to, _ = _yesterday_window_bounds(now)
    # Окна лидов/квалов — ровно те, за которые их считал сборщик (AMO).
    today_window = TimeWindow(
        start=datetime.fromtimestamp(today_from, tz=_TZ_LOCAL),
        end=datetime.fromtimestamp(today_to, tz=_TZ_LOCAL) + timedelta(microseconds=1),
        timezone_name="Etc/GMT-5",
        semantic="amo_leads_since_account_day_start",
    )
    yesterday_window = TimeWindow(
        start=datetime.fromtimestamp(yesterday_from, tz=_TZ_LOCAL),
        end=datetime.fromtimestamp(yesterday_to, tz=_TZ_LOCAL) + timedelta(seconds=1),
        timezone_name="Etc/GMT-5",
        semantic="closed_local_day",
    )
    # Окна расхода FB — СУТКИ КАБИНЕТА целиком, а не «12:00 → сейчас» и не
    # локальные сутки. Так их и меряет _get_fb_spend_for_day (insights
    # time_range since=until=дата считается по времени аккаунта), и только
    # такое окно Graph умеет доказать: у insights дневная гранулярность,
    # частичный интервал источник отказывается подтверждать (см.
    # approval_source_facebook._load_account_insights — окно, чьи границы не
    # совпали с полуночью кабинета, молча пропускается → EVIDENCE_MISSING на
    # window_start/window_end/fb_spend). Раньше claim подписывал расход суток
    # кабинета локальными сутками — окно не совпадало с измеряемой величиной.
    today_fb_window, yesterday_fb_window = _fb_account_day_windows(now)
    action_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    action_window = TimeWindow(
        start=action_start,
        end=max(now, action_start + timedelta(microseconds=1)),
        timezone_name="Etc/GMT-5",
        semantic="confirmed_actions_today",
    )

    fields: list[ReportField] = []
    claims: list[FactClaim] = []
    section_fields = {
        "facebook_today": [],
        "outcomes_today": [],
        "facebook_yesterday": [],
        "outcomes_yesterday": [],
        "actions_today": [],
    }

    for prefix, section_id, window, fb_window, bucket in (
        (
            "today",
            "facebook_today",
            today_window,
            today_fb_window,
            windows.get("today", {}),
        ),
        (
            "yesterday",
            "facebook_yesterday",
            yesterday_window,
            yesterday_fb_window,
            windows.get("yesterday", {}),
        ),
    ):
        _append_fact(
            fields,
            claims,
            section_fields,
            field_id=f"{prefix}.window_start",
            section_id=section_id,
            category=FactCategory.WINDOW_BOUND,
            label=FieldLabelTemplate.WINDOW_START,
            field_format=FieldFormat.DATETIME,
            subject=account,
            metric=Metric.WINDOW_START,
            value=fb_window.start.isoformat(),
            source=SourceSystem.FACEBOOK,
            window=fb_window,
        )
        _append_fact(
            fields,
            claims,
            section_fields,
            field_id=f"{prefix}.window_end",
            section_id=section_id,
            category=FactCategory.WINDOW_BOUND,
            label=FieldLabelTemplate.WINDOW_END,
            field_format=FieldFormat.DATETIME,
            subject=account,
            metric=Metric.WINDOW_END,
            value=fb_window.end.isoformat(),
            source=SourceSystem.FACEBOOK,
            window=fb_window,
        )
        _append_fact(
            fields,
            claims,
            section_fields,
            field_id=f"{prefix}.fb_spend",
            section_id=section_id,
            category=FactCategory.BUSINESS_METRIC,
            label=FieldLabelTemplate.SPEND,
            field_format=FieldFormat.USD,
            subject=account,
            metric=Metric.SPEND,
            value=_as_decimal(bucket.get("fb_spend")),
            source=SourceSystem.FACEBOOK,
            window=fb_window,
            currency="USD",
        )

        outcome_section = f"outcomes_{prefix}"
        _append_fact(
            fields,
            claims,
            section_fields,
            field_id=f"{prefix}.amo_leads",
            section_id=outcome_section,
            category=FactCategory.BUSINESS_METRIC,
            label=FieldLabelTemplate.LEADS,
            field_format=FieldFormat.INTEGER,
            subject=account,
            metric=Metric.LEADS,
            value=bucket.get("leads"),
            source=SourceSystem.AMO,
            window=window,
        )
        _append_fact(
            fields,
            claims,
            section_fields,
            field_id=f"{prefix}.amo_quals",
            section_id=outcome_section,
            category=FactCategory.BUSINESS_METRIC,
            label=FieldLabelTemplate.QUALS,
            field_format=FieldFormat.INTEGER,
            subject=account,
            metric=Metric.QUALS,
            value=bucket.get("quals"),
            source=SourceSystem.AMO,
            window=window,
        )

    confirmed_count, history_state = _history_snapshot(action_window)
    _append_fact(
        fields,
        claims,
        section_fields,
        field_id="actions.confirmed_count",
        section_id="actions_today",
        category=FactCategory.HISTORY_AUDIT,
        label=FieldLabelTemplate.ACTION_COUNT,
        field_format=FieldFormat.INTEGER,
        subject=checker,
        metric=Metric.ACTION_COUNT,
        value=confirmed_count,
        source=SourceSystem.CHECKER_AUDIT,
        window=action_window,
    )
    _append_fact(
        fields,
        claims,
        section_fields,
        field_id="actions.history_state",
        section_id="actions_today",
        category=FactCategory.HISTORY_AUDIT,
        label=FieldLabelTemplate.HISTORY_STATE,
        field_format=FieldFormat.STATUS,
        subject=checker,
        metric=Metric.HISTORY_STATE,
        value=history_state,
        source=SourceSystem.CHECKER_AUDIT,
        window=action_window,
    )

    sections = (
        ReportSection(
            "facebook_today",
            SectionTemplate.FACEBOOK,
            tuple(section_fields["facebook_today"]),
            today_fb_window,
        ),
        ReportSection(
            "outcomes_today",
            SectionTemplate.OUTCOMES,
            tuple(section_fields["outcomes_today"]),
            today_window,
        ),
        ReportSection(
            "facebook_yesterday",
            SectionTemplate.FACEBOOK,
            tuple(section_fields["facebook_yesterday"]),
            yesterday_fb_window,
        ),
        ReportSection(
            "outcomes_yesterday",
            SectionTemplate.OUTCOMES,
            tuple(section_fields["outcomes_yesterday"]),
            yesterday_window,
        ),
        ReportSection(
            "actions_today",
            SectionTemplate.ACTIONS,
            tuple(section_fields["actions_today"]),
            action_window,
        ),
    )
    payload = TypedReportPayload(
        template=ReportTemplate.EVENING,
        sections=sections,
        fields=tuple(fields),
        generated_at=now,
        mixed_windows_explicit=True,
    )
    claims_tuple = tuple(claims)
    return ReportCheckRequest(
        correlation_id=f"evening:{now.date().isoformat()}:{uuid.uuid4()}",
        payload=payload,
        claims=claims_tuple,
        manifest_sha256=report_manifest_sha256(payload, claims_tuple),
    )


def _google_for_day(day_iso: str) -> dict:
    """Расход Google за конкретный день + маркер «данных ещё нет».

    Различаем «0» и «нет данных»:
    spend=None и missing=True, когда вкладку дня подрядчик ещё не залил (не 0!).

    Returns:
        {"spend": float|None, "missing": bool, "last_known": (iso, val)|None}.
    """
    from services.google_spend import (
        get_daily_google_spend,
        get_last_known_google_spend,
    )

    val = get_daily_google_spend(day_iso)
    if val is None:
        return {
            "spend": None,
            "missing": True,
            "last_known": get_last_known_google_spend(day_iso),
        }
    return {"spend": val, "missing": False, "last_known": None}


def _google_missing_note(last_known: tuple | None) -> str:
    """Строка «Google: данных ещё нет · последний известный день DD.MM: $X»
    Без истории — короткое «данных ещё нет»."""
    if last_known:
        last_iso, last_val = last_known
        last_dd_mm = date.fromisoformat(last_iso).strftime("%d.%m")
        return (
            f"Google: данных ещё нет · последний известный день "
            f"{last_dd_mm}: {fmt_money(last_val, '$')}"
        )
    return "Google: данных ещё нет"


def _fmt_spend_with_google(bucket: dict) -> str:
    """Строка расхода FB+Google для секции «Деньги» с честным «данных ещё нет».

    - FB не отдал день (fb_spend=None) → «FB: данных ещё нет» + строка Google,
      если снимок Google за день есть (симметрично Google-ветке ниже).
    - total_spend=None без fb_missing (источник упал) → «нет данных».
    - Google-снимок дня отсутствует → «FB $A · Google: данных ещё нет · …».
    - Иначе — как раньше: «$C (FB $A + Google $B)».
    """
    if bucket.get("fb_missing"):
        if bucket.get("google_missing"):
            return f"FB: данных ещё нет · {_google_missing_note(bucket.get('google_last_known'))}"
        if bucket.get("google_spend") is not None:
            return (
                "FB: данных ещё нет · "
                f"Google {fmt_money(bucket['google_spend'], '$')}"
            )
        return "FB: данных ещё нет"
    if bucket.get("total_spend") is None:
        return "нет данных"
    if bucket.get("google_missing"):
        note = _google_missing_note(bucket.get("google_last_known"))
        return f"FB {fmt_money(bucket['fb_spend'], '$')} · {note}"
    return (
        f"{fmt_money(bucket['total_spend'], '$')} "
        f"(FB {fmt_money(bucket['fb_spend'], '$')} + Google {fmt_money(bucket['google_spend'], '$')})"
    )


def _fetch_decisions_for_range(date_from: str, date_to: str) -> list[dict]:
    """Тянет решения автопилота за произвольный диапазон из decisions_repo.

    Импорт через sys.modules — чтобы patch.dict в тестах работал корректно
    (как было в старой реализации). При ошибке — пустой список, отчёт не падает.
    """
    try:
        import sys as _sys
        import importlib as _importlib

        _importlib.import_module("agent.repositories.decisions_repo")
        decisions_repo = _sys.modules["agent.repositories.decisions_repo"]

        result = decisions_repo.get_decisions_history(
            tenant_id="default",
            date_from=date_from,
            date_to=date_to,
            limit=500,
        )
        decisions = result.get("decisions", [])
        return [
            d for d in decisions if d.get("confirmed_by") in _AUTOPILOT_CONFIRMED_BY
        ]
    except Exception as exc:
        logger.warning(
            "evening_report: решения за %s..%s недоступны — %s", date_from, date_to, exc
        )
        return []


def _fetch_contour_pauses(now: datetime) -> list[dict]:
    """Паузы дня из НОВОГО контура одобрений (owner_action_attempts CONFIRMED).

    Регрессия: с переходом пауз на контур предложений старая
    таблица ``decisions`` перестала наполняться, и вечерний отчёт молча показывал
    «действий не было», пока автопилот продолжал выключать рекламу.
    Строки приводятся к формату decisions_repo, чтобы вердикт, секция «Что
    сделал бот» и кнопки «Вернуть» работали без переделки. Богатые поля (расход,
    лиды, квал, причина) — из журнала автономных пауз, иначе из creative_kb.
    Любая ошибка = пустой список: отчёт важнее одной секции.
    """
    try:
        import sqlite3 as _sqlite

        from services.creative_intelligence import DB_PATH

        if DB_PATH is None:
            return []
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = day_start.astimezone(timezone.utc).isoformat()
        end_utc = now.astimezone(timezone.utc).isoformat()
        conn = _sqlite.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = _sqlite.Row
        try:
            rows = conn.execute(
                """
                SELECT a.resource_id AS ad_id, a.completed_at,
                       COALESCE(d.decision_source, 'OWNER') AS source
                FROM owner_action_attempts a
                LEFT JOIN owner_action_decisions d ON d.decision_id = a.decision_id
                WHERE a.operation_kind = 'PAUSE_AD' AND a.state = 'CONFIRMED'
                  AND a.completed_at >= ? AND a.completed_at <= ?
                ORDER BY a.completed_at
                """,
                (start_utc, end_utc),
            ).fetchall()
            kb: dict[str, dict] = {}
            if rows:
                placeholders = ",".join("?" for _ in rows)
                try:
                    for r in conn.execute(
                        f"SELECT ad_id, ad_name, spend, leads, cpl, qual_pct FROM creative_kb "
                        f"WHERE ad_id IN ({placeholders})",
                        [str(r["ad_id"]) for r in rows],
                    ):
                        kb[str(r["ad_id"])] = dict(r)
                except _sqlite.OperationalError:
                    kb = {}
        finally:
            conn.close()

        journal: dict[str, dict] = {}
        try:
            from services.autonomous_pause import load_state as _load_auto_state

            for item in (_load_auto_state().get("journal") or []):
                if isinstance(item, dict) and item.get("ad_id"):
                    journal[str(item["ad_id"])] = item
        except Exception as exc:  # noqa: BLE001 — журнал не обязателен
            logger.debug("evening_report: журнал автономии недоступен — %s", exc)

        result: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            ad_id = str(r["ad_id"] or "")
            if not ad_id or ad_id in seen:
                continue
            seen.add(ad_id)
            j = journal.get(ad_id) or {}
            k = kb.get(ad_id) or {}
            try:
                paused_at = datetime.fromisoformat(str(r["completed_at"])).astimezone(
                    _TZ_LOCAL
                )
            except Exception:
                paused_at = now
            result.append(
                {
                    "ad_id": ad_id,
                    "ad_name": str(j.get("name") or k.get("ad_name") or ad_id),
                    "action": "PAUSED",
                    "reason": str(j.get("business_reason") or j.get("reason") or ""),
                    "confirmed_by": "autopilot" if str(r["source"]).upper() == "SYSTEM" else "owner_telegram",
                    "created_at": paused_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "spend": j.get("spend") if j.get("spend") is not None else k.get("spend"),
                    "leads": j.get("leads") if j.get("leads") is not None else k.get("leads"),
                    "cpl": j.get("cpl") if j.get("cpl") is not None else k.get("cpl"),
                    "qual_pct": j.get("qual_pct") if j.get("qual_pct") not in (None, "None") else k.get("qual_pct"),
                }
            )
        return result
    except Exception as exc:  # noqa: BLE001 — секция отчёта не роняет отчёт
        logger.warning("evening_report: паузы контура недоступны — %s", exc)
        return []


def _fetch_today_decisions(now: datetime) -> list[dict]:
    """Решения бота за сегодня (локальные сутки): старая таблица decisions +
    паузы нового контура одобрений (дедуп по ad_id, контур побеждает)."""
    today_start = f"{now.date().isoformat()} 00:00:00"
    today_end = now.strftime("%Y-%m-%d %H:%M:%S")
    legacy = _fetch_decisions_for_range(today_start, today_end)
    contour = _fetch_contour_pauses(now)
    if not contour:
        return legacy
    contour_ids = {d["ad_id"] for d in contour}
    merged = [
        d for d in legacy
        if not (d.get("action") == "PAUSED" and str(d.get("ad_id")) in contour_ids)
    ]
    merged.extend(contour)
    return merged


def _leads_quals_payments(from_ts: int, to_ts: int) -> tuple[int, int, int]:
    """Считает (лиды, квалы, оплаты) из AMO за окно [from_ts, to_ts] (unix-время).

    integrations.amo.get_leads_window фильтрует по created_at — тем же полем,
    что и время создания лида в AMO, окно можно задавать любой длины (не
    обязательно ровно сутки), это и используется для окна «день кабинета
    с 12:00». Бросает исключение наверх — caller решает, как показать ошибку.
    """
    from integrations.amo import get_leads_window, classify_lead

    leads = get_leads_window(from_ts, to_ts)
    total = len(leads)
    quals = sum(1 for lead in leads if classify_lead(lead) == "квал")
    payments = sum(1 for lead in leads if classify_lead(lead) == "оплата")
    return total, quals, payments


def _today_window_bounds(now: datetime) -> tuple[int, int]:
    """Границы «сегодня» = окно ДНЯ КАБИНЕТА FB: с 12:00 по локальному времени сегодня до now.

    Кабинет FB живёт по времени Америки (LA) — его сутки в insights API
    начинаются в полночь LA, что соответствует 12:00 по локальному времени. Если now раньше 12:00 (отчёт шлётся не по
    расписанию/ручной вызов до полудня) — окно ещё не открылось, берём
    от 12:00 ВЧЕРА до now (последний завершённый старт окна).
    """
    today_noon = now.replace(
        hour=_FB_ACCOUNT_DAY_START_HOUR, minute=0, second=0, microsecond=0
    )
    window_start = today_noon if now >= today_noon else today_noon - timedelta(days=1)
    return int(window_start.timestamp()), int(now.timestamp())


def _yesterday_window_bounds(now: datetime) -> tuple[int, int, str]:
    """Границы «вчера» — ПОЛНЫЕ локальные сутки [00:00:00, 23:59:59] + ISO-дата.

    Совпадает с morning_digest._yesterday_bounds (полный закрытый день),
    но возвращает unix-timestamps под integrations.amo.get_leads_window.
    """
    yesterday_date = (now - timedelta(days=1)).date()
    start = datetime.combine(yesterday_date, datetime.min.time(), tzinfo=_TZ_LOCAL)
    end = start.replace(hour=23, minute=59, second=59)
    return int(start.timestamp()), int(end.timestamp()), yesterday_date.isoformat()


def _compute_money_windows(now: datetime) -> dict:
    """Считает оба честных денежных окна: «сегодня» (день кабинета с 12:00) и
    «вчера» (полный закрытый день). Используется и в _section_money, и в вердикте
    — вычисления не дублируем.

    Расход FB — ТОЛЬКО через morning_digest._get_fb_spend_for_day, НИКОГДА через
    budget_scaler.get_fb_week_spend (та читает многодневный кеш и для однодневного
    окна отдаёт завышенную сумму — см. докстринг _get_fb_spend_for_day). Для
    «сегодня» вызываем _get_fb_spend_for_day(сегодня) — FB insights time_range
    считает по времени АККАУНТА (LA), поэтому 'сегодня' в терминах FB и есть
    открытое окно 12:00 по локальному времени -> now. Для «вчера» — тот же вызов на вчерашнюю
    ISO-дату, это уже ЗАКРЫТЫЙ день кабинета (12:00 вчера -> 12:00 сегодня).

    Возвращает dict:
    {
        "today": {"fb_spend"|None, "google_spend"|None, "total_spend"|None,
                  "leads"|None, "quals"|None, "payments"|None, "qual_pct"|None,
                  "cpl"|None, "cost_per_qual"|None},
        "yesterday": {то же самое, плюс "date_str"},
    }
    Каждое поле None при ошибке источника (честно «нет данных», не 0).
    """
    from services.morning_digest import _get_fb_spend_for_day

    today_str = now.date().isoformat()
    result: dict = {"today": {}, "yesterday": {}}

    # --- Сегодня: день кабинета с 12:00 по локальному времени ---
    today_bucket = result["today"]
    try:
        fb_spend = _get_fb_spend_for_day(today_str)
        g = _google_for_day(today_str)
        today_bucket["fb_spend"] = fb_spend
        today_bucket["google_spend"] = g["spend"]
        today_bucket["google_missing"] = g["missing"]
        today_bucket["google_last_known"] = g["last_known"]
        # fb_spend=None — FB ещё не отдал строку за день кабинета. Это «данных
        # ещё нет», а не $0: тотал тоже None, иначе отчёт заявил бы расход
        # $0 на пустом ответе Graph (чекер бракует это NULL_COERCED_TO_ZERO).
        today_bucket["fb_missing"] = fb_spend is None
        # google_spend может быть None (вкладки дня ещё нет) — в total берём как 0,
        # но missing-флаг заставит секцию «Деньги» показать «данных ещё нет».
        today_bucket["total_spend"] = (
            None if fb_spend is None else fb_spend + (g["spend"] or 0.0)
        )
    except Exception as exc:
        logger.warning(
            "evening_report: расход «сегодня» (день кабинета) недоступен — %s", exc
        )
        today_bucket["fb_spend"] = today_bucket["google_spend"] = today_bucket[
            "total_spend"
        ] = None
        today_bucket["fb_missing"] = True
        today_bucket["google_missing"] = False
        today_bucket["google_last_known"] = None

    try:
        from_ts, to_ts = _today_window_bounds(now)
        leads, quals, payments = _leads_quals_payments(from_ts, to_ts)
        today_bucket["leads"] = leads
        today_bucket["quals"] = quals
        today_bucket["payments"] = payments
        today_bucket["qual_pct"] = (quals / leads * 100) if leads > 0 else 0.0
    except Exception as exc:
        logger.warning("evening_report: лиды «сегодня» (с 12:00) недоступны — %s", exc)
        today_bucket["leads"] = today_bucket["quals"] = today_bucket["payments"] = None
        today_bucket["qual_pct"] = None

    today_bucket["cpl"] = None
    today_bucket["cost_per_qual"] = None
    if today_bucket.get("total_spend") is not None and today_bucket.get("leads"):
        today_bucket["cpl"] = today_bucket["total_spend"] / today_bucket["leads"]
    if today_bucket.get("total_spend") is not None and today_bucket.get("quals"):
        today_bucket["cost_per_qual"] = (
            today_bucket["total_spend"] / today_bucket["quals"]
        )

    # --- Вчера: полный закрытый день ---
    y_from_ts, y_to_ts, y_date_str = _yesterday_window_bounds(now)
    yesterday_bucket = result["yesterday"]
    yesterday_bucket["date_str"] = y_date_str
    try:
        fb_spend = _get_fb_spend_for_day(y_date_str)
        g = _google_for_day(y_date_str)
        yesterday_bucket["fb_spend"] = fb_spend
        yesterday_bucket["google_spend"] = g["spend"]
        yesterday_bucket["google_missing"] = g["missing"]
        yesterday_bucket["google_last_known"] = g["last_known"]
        yesterday_bucket["fb_missing"] = fb_spend is None
        yesterday_bucket["total_spend"] = (
            None if fb_spend is None else fb_spend + (g["spend"] or 0.0)
        )
    except Exception as exc:
        logger.warning(
            "evening_report: расход «вчера» (закрытый день) недоступен — %s", exc
        )
        yesterday_bucket["fb_spend"] = yesterday_bucket[
            "google_spend"
        ] = yesterday_bucket["total_spend"] = None
        yesterday_bucket["fb_missing"] = True
        yesterday_bucket["google_missing"] = False
        yesterday_bucket["google_last_known"] = None

    try:
        leads, quals, payments = _leads_quals_payments(y_from_ts, y_to_ts)
        yesterday_bucket["leads"] = leads
        yesterday_bucket["quals"] = quals
        yesterday_bucket["payments"] = payments
        yesterday_bucket["qual_pct"] = (quals / leads * 100) if leads > 0 else 0.0
    except Exception as exc:
        logger.warning("evening_report: лиды «вчера» недоступны — %s", exc)
        yesterday_bucket["leads"] = yesterday_bucket["quals"] = yesterday_bucket[
            "payments"
        ] = None
        yesterday_bucket["qual_pct"] = None

    yesterday_bucket["cpl"] = None
    yesterday_bucket["cost_per_qual"] = None
    if yesterday_bucket.get("total_spend") is not None and yesterday_bucket.get(
        "leads"
    ):
        yesterday_bucket["cpl"] = (
            yesterday_bucket["total_spend"] / yesterday_bucket["leads"]
        )
    if yesterday_bucket.get("total_spend") is not None and yesterday_bucket.get(
        "quals"
    ):
        yesterday_bucket["cost_per_qual"] = (
            yesterday_bucket["total_spend"] / yesterday_bucket["quals"]
        )

    return result


def _section_money(now: datetime, windows: dict) -> list[str]:
    """Секция «Деньги» v2 — ДВЕ отдельные честные строки, без смешивания окон.

    «Сегодня (день кабинета с 12:00)»: расход+лиды за ещё открытое окно кабинета —
    дозагружается, финал будет в утреннем дайджесте.
    «Вчера (полный день)»: расход+лиды за уже закрытые сутки — CPL и квал% здесь
    финальные и сравнимые день-к-дню.
    Никакого единого CPL из разных окон — каждая строка считает СВОЙ CPL внутри
    своего же окна.
    Юнитка — переиспользуем morning_digest._section_unit_economics (не зависит от
    «вчера»/«сегодня», считает trailing-окно ДРР до субботы).
    """
    lines = ["<b>Деньги</b>"]

    today_w = windows.get("today", {})
    yesterday_w = windows.get("yesterday", {})

    # --- Сегодня (день кабинета с 12:00) ---
    today_money = _fmt_spend_with_google(today_w)
    if today_w.get("leads") is None:
        today_leads = "нет данных"
    else:
        today_leads = f"{today_w['leads']}"
    today_cpl = (
        fmt_money(today_w.get("cpl"), "$") if today_w.get("cpl") is not None else "—"
    )
    lines.append(
        f"💸 Сегодня (день кабинета с {_FB_ACCOUNT_DAY_START_HOUR}:00): "
        f"расход {today_money} · лиды с {_FB_ACCOUNT_DAY_START_HOUR}:00 по AMO — {today_leads} · CPL {today_cpl}"
    )

    # --- Вчера (полный день) ---
    y_date_str = yesterday_w.get("date_str", "")
    y_money = _fmt_spend_with_google(yesterday_w)
    if yesterday_w.get("leads") is None:
        y_leads = "нет данных"
    else:
        y_leads = f"{yesterday_w['leads']}"
    y_cpl = (
        fmt_money(yesterday_w.get("cpl"), "$")
        if yesterday_w.get("cpl") is not None
        else "—"
    )
    y_qual_pct = yesterday_w.get("qual_pct")
    y_qual_str = f"{y_qual_pct:.0f}%" if y_qual_pct is not None else "—"
    lines.append(
        f"💸 Вчера ({y_date_str}, полный день): "
        f"расход {y_money} · лиды {y_leads} · CPL {y_cpl} · квал {y_qual_str}"
    )

    # --- Юнитка (переиспользуем секцию morning_digest — не дублируем логику) ---
    try:
        from services.morning_digest import _section_unit_economics

        unit_lines = _section_unit_economics(now)
        for line in unit_lines:
            lines.append(f"📊 {line}")
    except Exception as exc:
        logger.warning("evening_report: юнитка недоступна — %s", exc)
        lines.append("📊 Юнитка: нет данных")

    return lines


def _section_verdict(now: datetime, windows: dict, decisions: list[dict]) -> str:
    """Строка-вердикт дня — первая строка после заголовка (пункт 2 фидбека владельца).

    Человеческим языком, с числами: сколько объявлений выключил бот сегодня,
    квал% сегодня vs вчера (+/- п.п.), цена квала сегодня vs вчера.
    Сравнения ТОЛЬКО в валидных окнах: квал% — локальные сутки/окно с 12:00
    (оба относительные метрики, окно другой длины не искажает %), расход и
    производные от него (цена квала) — сравниваем оба окна закрытые по своей
    природе (сегодняшнее ещё дозагружается, помечено отдельно в футере).
    Если данных недостаточно для дельты — показываем то, что есть, без деления на 0.
    """
    paused_count = sum(1 for d in decisions if d.get("action") == "PAUSED")

    today_w = windows.get("today", {})
    yesterday_w = windows.get("yesterday", {})

    parts = []
    if paused_count:
        parts.append(
            f"Бот выключил {paused_count} слаб{'ое' if paused_count == 1 else 'ых'} объявлени{'е' if paused_count == 1 else 'й'}"
        )
    else:
        parts.append("Бот сегодня ничего не выключил")

    today_qual_pct = today_w.get("qual_pct")
    y_qual_pct = yesterday_w.get("qual_pct")
    if today_qual_pct is not None and y_qual_pct is not None:
        delta = today_qual_pct - y_qual_pct
        sign = "+" if delta >= 0 else ""
        parts.append(f"квал {today_qual_pct:.0f}% ({sign}{delta:.0f} п.п. к вчера)")
    elif today_qual_pct is not None:
        parts.append(f"квал {today_qual_pct:.0f}%")

    today_cost_per_qual = today_w.get("cost_per_qual")
    y_cost_per_qual = yesterday_w.get("cost_per_qual")
    if today_cost_per_qual is not None and y_cost_per_qual is not None:
        parts.append(
            f"цена квала {fmt_money(today_cost_per_qual, '$')} "
            f"(вчера {fmt_money(y_cost_per_qual, '$')})"
        )
    elif today_cost_per_qual is not None:
        parts.append(f"цена квала {fmt_money(today_cost_per_qual, '$')}")

    return "; ".join(parts) + "."


def _city_from_ad_name(ad_name: str) -> str | None:
    """Извлекает город из имени объявления по паттерну «Город | Тема» (как в
    services.insights.extract_topic — переиспользуем тот же паттерн разбора,
    здесь нужна первая часть, не вторая)."""
    if not ad_name or "|" not in ad_name:
        return None
    city = ad_name.split("|", 1)[0].strip()
    return city or None


def _decision_to_pause_block_ad(d: dict) -> dict:
    """Преобразует запись decisions_repo в формат ad для autopilot._format_pause_block.

    decisions хранит: ad_name, reason, spend, leads, cpl, qual_pct (payments НЕ
    хранится решениями — честно «—» внутри _format_pause_block, а не выдуманный 0).
    Причину прогоняем через autopilot._humanize_pause_reason — человеческий язык
    (пункт 3 фидбека владельца), а не сырой технический reason.

    Город выносим ОТДЕЛЬНО из имени (autopilot._format_pause_block печатает
    «<b>{city}</b> | {name}» — при передаче полного ad_name город дублировался
    бы визуально: «CityA | CityA | Тема»), поэтому в name передаём остаток
    имени ПОСЛЕ «Город | », без дублирования.
    """
    from services.autopilot import _humanize_pause_reason

    ad_name = d.get("ad_name", "") or ""
    city = _city_from_ad_name(ad_name)
    name_without_city = (
        ad_name.split("|", 1)[1].strip() if city and "|" in ad_name else ad_name
    )
    return {
        "name": name_without_city,
        "city": city,
        "spend": d.get("spend"),
        "leads": d.get("leads"),
        "cpl": d.get("cpl"),
        "qual_pct": d.get("qual_pct"),
        "payments": None,
        "reason": _humanize_pause_reason(d.get("reason", "") or ""),
    }


def _section_bot_actions(
    now: datetime, decisions: list[dict]
) -> tuple[list[str], list[dict]]:
    """Секция «Что сделал бот» v2: паузы и удержания — ПОЛНЫМИ блоками (пункт 3
    фидбека владельца — не топ-3, ВСЕ паузы дня), подъёмы бюджета кратко (как
    было), запуски, карточки ТЗ. Нулевые пункты не показываются.

    Полный блок пауз переиспользует autopilot._format_pause_block — тот же
    формат, что и в realtime-отчётах автопилота: город, имя, 💸 расход,
    👥 квал+оплаты, 📉 причина человеческим языком.

    Возвращает (строки секции, paused_ads для кнопок «Вернуть»).
    """
    from services.autopilot import _format_pause_block, _format_held_section

    lines = ["<b>Что сделал бот</b>"]
    paused_ads: list[dict] = []
    any_action = False

    # --- Паузы: ВСЕ решения дня, полными блоками (не топ-3) ---
    paused = [d for d in decisions if d.get("action") == "PAUSED"]
    if paused:
        any_action = True
        lines.append(f"⏸ <b>Паузы: {len(paused)}</b>")
        for i, d in enumerate(paused, start=1):
            block_ad = _decision_to_pause_block_ad(d)
            lines.append(_format_pause_block(i, block_ad))
        # Кнопки «Вернуть» — только для пауз от autopilot (не owner_telegram/cleaner)
        for d in paused:
            if d.get("confirmed_by") == "autopilot":
                paused_ads.append(
                    {"ad_id": d.get("ad_id", ""), "ad_name": d.get("ad_name", "")}
                )
        # Метрика Стража за 7 дней: как быстро режется слив и сколько недельного
        # расхода остановлено (раньше считалась по мёртвой таблице и в
        # отчёт не попадала вовсе).
        try:
            from services.guardian import guardian_time_to_pause_stats

            stats = guardian_time_to_pause_stats(days=7)
            if stats.get("pauses"):
                hours = stats.get("avg_hours_to_pause")
                hours_txt = f"в среднем {hours:.0f} ч" if hours is not None else "время не измерено"
                lines.append(
                    f"⏱ За 7 дней: {stats['pauses']} пауз · слив→пауза {hours_txt} · "
                    f"остановлено ~{fmt_money(stats.get('saved_usd_week') or 0, '$')}/нед"
                )
        except Exception as exc:  # noqa: BLE001 — метрика не роняет секцию
            logger.debug("evening_report: метрика Стража недоступна — %s", exc)

    # --- Удержания (held) — активные holds из autopilot_hold_state.json,
    # поставленные СЕГОДНЯ (held_at). Дёшево: чтение одного JSON-файла, без сети.
    # Полный блок через autopilot._format_held_section (тот же формат: имя,
    # дата окончания, ROMI, расход, квал) — пункт 3 фидбека: «Удержания — тем
    # же форматом». ---
    try:
        from services.autopilot_hold import load_hold_state

        hold_state = load_hold_state()
        holds = hold_state.get("holds", {})
        today_str = now.date().isoformat()
        held_today = [
            entry
            for entry in holds.values()
            if isinstance(entry, dict)
            and str(entry.get("held_at", "")).startswith(today_str)
        ]
        if held_today:
            any_action = True
            lines.append(_format_held_section(held_today))
    except Exception as exc:
        logger.debug("evening_report: удержания недоступны — %s", exc)

    # --- Подъёмы бюджета (кратко, как было — пункт 4 фидбека) ---
    raised = [d for d in decisions if d.get("action") == "BUDGET_RAISED"]
    if raised:
        any_action = True
        lines.append(f"⬆️ Подняли бюджет: {len(raised)}")
        for d in raised[:_TOP_RAISED_SHOWN]:
            raw_reason = d.get("reason", "") or ""
            # raw_reason содержит "{adset_name}: $X→$Y (+Z%, сегодня A%/B%), N оплат за Mдн" —
            # берём часть до закрывающей скобки процента включительно (не по первой запятой:
            # она стоит ВНУТРИ скобок "(+15%, сегодня...)" и режет "+15%" до "(+15%",
            # теряя закрывающую ")"). Ищем "), " — конец скобки с процентом роста.
            cut_idx = raw_reason.find("), ")
            short_reason = raw_reason[: cut_idx + 1] if cut_idx != -1 else raw_reason
            lines.append(f"  • {html.escape(short_reason) if short_reason else '—'}")

    # --- Запуски (auto_launch_state.json, launched_ever за сегодня) ---
    try:
        import json as _json
        from services.auto_launch import _launched_at

        if _LAUNCH_STATE_PATH.exists():
            state = _json.loads(_LAUNCH_STATE_PATH.read_text(encoding="utf-8"))
            launched_ever = state.get("launched_ever", {})
            today_str = now.date().isoformat()
            # _launched_at читает дату из ОБОИХ форматов записи (старый — просто
            # строка iso_datetime; новый — {"at":..., "name":...}) — старые записи
            # без name не должны ломать подсчёт.
            launches_today = sum(
                1
                for entry in launched_ever.values()
                if _launched_at(entry).startswith(today_str)
            )
            if launches_today:
                any_action = True
                lines.append(f"🚀 Запусков: {launches_today}")
    except Exception as exc:
        logger.debug("evening_report: запуски за сегодня недоступны — %s", exc)

    # --- Карточки ТЗ (brief_gen_state.json, generated_signatures за сегодня) ---
    try:
        import json as _json

        from services.brief_generator import _latest_brief_run_date

        if _BRIEF_STATE_PATH.exists():
            state = _json.loads(_BRIEF_STATE_PATH.read_text(encoding="utf-8"))
            # Регрессия: раньше читали единый last_run_date, теперь
            # брифогенератор пишет last_scheduled_run_date/last_manual_run_date
            # раздельно (см. services/brief_generator.py) — _latest_brief_run_date
            # берёт более свежую из двух меток, fallback на legacy last_run_date.
            last_run_date = _latest_brief_run_date(state)
            today_str = now.date().isoformat()
            # last_run_date — полный ISO-таймстамп (datetime.now().isoformat()),
            # сравниваем только дату (первые 10 символов), не строку целиком —
            # тот же паттерн, что held_at.startswith(today_str) выше в этой функции.
            if last_run_date and last_run_date[:10] == today_str:
                briefs_today = len(state.get("generated_signatures", []))
                if briefs_today:
                    any_action = True
                    lines.append(f"🃏 Карточек ТЗ: {briefs_today}")
    except Exception as exc:
        logger.debug("evening_report: карточки ТЗ за сегодня недоступны — %s", exc)

    if not any_action:
        lines.append("Сегодня действий не было.")

    return lines, paused_ads


def _section_doubts(now: datetime) -> list[str]:
    """Секция «Сомнения» — сработавшие сегодня doubt-триггеры Budget Scaler.

    Протокол сомнений (services/budget_scaler.py::_evaluate_doubt_triggers) на
    каждый прогон с сомнением пишет запись в журнал data/doubt_log.json
    (services.doubt_log.append_doubt_entry). Здесь читаем записи за СЕГОДНЯ
    (локальные сутки, как и остальные секции отчёта) и показываем маркированным
    списком причин каждого прогона. Пустой день (нет записей) → секция скрыта
    целиком — вызывающий код (build_evening_report) проверяет пустой список.
    """
    try:
        from services.doubt_log import get_doubt_entries_for_date

        today_str = now.date().isoformat()
        entries = get_doubt_entries_for_date(today_str)
    except Exception as exc:
        logger.warning("evening_report: журнал сомнений недоступен — %s", exc)
        return []

    if not entries:
        return []

    lines = ["<b>Сомнения</b>"]
    for entry in entries:
        decision = entry.get("decision", "") or "—"
        lines.append(f"🤔 Решение: {html.escape(decision)}")
        for trigger in entry.get("triggers") or []:
            lines.append(f"  • {html.escape(str(trigger))}")
    return lines


def _section_tomorrow() -> list[str]:
    """Секция «Завтра»: слоты Стража, автозапуск 10:00, масштабирование 13:00.

    Переиспользуем morning_digest._section_today_plan() — тот же статический
    текст расписания кронов, дублировать не нужно.
    """
    try:
        from services.morning_digest import _section_today_plan

        plan_lines = _section_today_plan()
        # Первая строка _section_today_plan — заголовок "Сегодня по плану:", убираем
        return ["<b>Завтра</b>"] + plan_lines[1:]
    except Exception as exc:
        logger.warning("evening_report: секция «Завтра» недоступна — %s", exc)
        return [
            "<b>Завтра</b>",
            "Слоты Стража 08–22 · авто-запуск 10:00 · масштабирование 13:00",
        ]


def _truncate_to_limit(lines: list[str]) -> str:
    """Склеивает секции и обрезает по лимиту Telegram (4096) — режем с конца по строкам.

    Как в _format_pause_report: если целиком не влезает, добавляем пометку об усечении.
    """
    text = "\n".join(lines)
    if len(text) <= _TELEGRAM_MAX_LEN:
        return text

    # Режем с конца построчно, пока не влезет + метка усечения
    marker = "\n…отчёт обрезан (см. дашборд)"
    budget = _TELEGRAM_MAX_LEN - len(marker)
    truncated_lines: list[str] = []
    running_len = 0
    for line in lines:
        add_len = len(line) + 1  # +1 за перевод строки
        if running_len + add_len > budget:
            break
        truncated_lines.append(line)
        running_len += add_len
    return "\n".join(truncated_lines) + marker


def build_evening_report(now: datetime | None = None) -> dict:
    """Строит вечерний отчёт за сегодня в новом читабельном блочном формате.

    Возвращает {"text": html_str, "buttons": list[list[tuple[str,str]]]}.
    Никогда не бросает исключений — при ошибках подставляет заглушки.
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_LOCAL)

    date_str = now.strftime("%d.%m")
    lines = [f"🌇 <b>Итог дня {date_str}</b>", ""]

    decisions = _fetch_today_decisions(now)

    # --- Деньги: считаем оба честных окна ОДИН раз (переиспользуют и вердикт,
    # и секция «Деньги» — не дублируем FB/AMO запросы) ---
    try:
        windows = _compute_money_windows(now)
    except Exception as exc:
        logger.warning("evening_report: денежные окна недоступны — %s", exc)
        windows = {"today": {}, "yesterday": {}}

    # --- Вердикт дня — первая строка после заголовка (пункт 2 фидбека владельца) ---
    try:
        verdict = _section_verdict(now, windows, decisions)
        lines.append(verdict)
        lines.append("")
    except Exception as exc:
        logger.warning("evening_report: вердикт дня упал — %s", exc)

    # --- Деньги ---
    try:
        lines.extend(_section_money(now, windows))
    except Exception as exc:
        logger.warning("evening_report: секция «Деньги» упала — %s", exc)
        lines.append("<b>Деньги</b>")
        lines.append("Нет данных")
    lines.append("")

    # --- Что сделал бот ---
    try:
        action_lines, paused_ads = _section_bot_actions(now, decisions)
        lines.extend(action_lines)
    except Exception as exc:
        logger.warning("evening_report: секция «Что сделал бот» упала — %s", exc)
        lines.append("<b>Что сделал бот</b>")
        lines.append("Сегодня действий не было.")
        paused_ads = []
    lines.append("")

    # --- Сомнения (пропускается целиком, если пусто — секция опциональна) ---
    doubt_lines = _section_doubts(now)
    if doubt_lines:
        lines.extend(doubt_lines)
        lines.append("")

    # --- Завтра ---
    try:
        lines.extend(_section_tomorrow())
    except Exception as exc:
        logger.warning("evening_report: секция «Завтра» упала — %s", exc)
        lines.append("<b>Завтра</b>")
        lines.append("Слоты Стража 08–22 · авто-запуск 10:00 · масштабирование 13:00")

    # --- Пометка о дозагрузке расхода (пункт 5 фидбека владельца) ---
    lines.append("")
    lines.append(
        "<i>Расход FB дозагружается — финальные цифры дня будут в утреннем дайджесте.</i>"
    )

    text = _truncate_to_limit(lines)

    # --- Кнопки ---
    buttons: list[list[tuple[str, str]]] = []
    buttons.append([("✅ Продолжай", "ack")])

    for ad in paused_ads[:_MAX_UNDO_BUTTONS]:
        ad_name = ad["ad_name"]
        short_name = ad_name[:25] if len(ad_name) > 25 else ad_name
        label = f"↩️ Вернуть {short_name}"
        callback = f"undo:{ad['ad_id']}"
        buttons.append([(label, callback)])

    check_request = build_evening_report_request(now, windows)
    return {
        "text": text,
        "buttons": buttons,
        "payload": check_request.payload,
        "claims": check_request.claims,
        "check_request": check_request,
    }


def send_evening_report() -> bool:
    """Проверяет typed facts и отправляет только deterministic checked render."""
    try:
        report = build_evening_report()
        from services.approval_checker import check_report
        from services.approval_report import render_checked_report
        from services.approval_telegram import send_checked_report, send_fact_free
        from services.approval_checker_models import FactFreeTemplate

        request = report["check_request"]
        result = check_report(request)
        rendered = render_checked_report(request, result)
        delivery = send_checked_report(rendered, channel="ads")
        if delivery.sent:
            return True
        if not result.audit_persisted:
            return send_fact_free(
                FactFreeTemplate.CHECKER_INTERNAL_ERROR,
                channel="ads",
            ).sent
        return False
    except Exception as exc:
        logger.warning("Ошибка при отправке вечернего отчёта: %s", exc)
        try:
            from services.approval_checker_models import FactFreeTemplate
            from services.approval_telegram import send_fact_free

            return send_fact_free(
                FactFreeTemplate.CHECKER_INTERNAL_ERROR,
                channel="ads",
                error_type=type(exc).__name__,
            ).sent
        except Exception as fallback_exc:
            logger.warning(
                "evening_report: safe facade недоступен — %s",
                type(fallback_exc).__name__,
            )
            return False


def should_send_report(now: datetime, last_report_date: date | None) -> bool:
    """Чистая функция: нужно ли слать отчёт прямо сейчас.

    Условия: час == 21 по локальному времени И today != last_report_date.

    Args:
        now: текущее время (с tzinfo или naive — сравниваем час напрямую)
        last_report_date: date-объект последней отправки (или None)
    """
    # Переводим в локальную TZ если есть tzinfo, иначе считаем уже локальным
    if now.tzinfo is not None:
        now_local = now.astimezone(_TZ_LOCAL)
    else:
        now_local = now

    if now_local.hour != 21:
        return False

    today = now_local.date()
    if last_report_date is not None and last_report_date == today:
        return False

    return True
