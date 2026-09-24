"""
Утренний дайджест автопилота.

Одно короткое (≤20 непустых строк) сообщение в Telegram утром (08:0x по локальному времени),
ДО первого слота Стража. Деловым языком: вчерашние деньги (расход FB+Google /
лиды / квалы / оплаты / факт ДРР vs план по субботнему правилу), что бот
сделал ночью и вчера, что сегодня по плану, аномалии если есть.

Read-only: никаких мутаций FB/AMO/бюджетов. Каждая секция — в своём try,
сбой одной секции не роняет весь дайджест (заглушка «нет данных»).
"""

import html
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

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

# Локальный часовой пояс (UTC+5 по умолчанию, настраивается здесь) — единый со всеми остальными модулями проекта
_TZ_LOCAL = timezone(timedelta(hours=5))

# Подтверждённые источники автопилота (как в evening_report._AUTOPILOT_CONFIRMED_BY,
# плюс cleaner/budget_pilot* — их решения тоже нужно агрегировать в дайджесте)
_AUTOPILOT_CONFIRMED_BY = {
    "autopilot",
    "autopilot_dry",
    "owner_telegram",
    "cleaner",
    "budget_pilot",
    "budget_pilot_dry",
}

# Путь к state-файлу брифогенератора (services/brief_generator.py) — модульная
# константа (как evening_report._BRIEF_STATE_PATH), чтобы тесты могли
# monkeypatch/patch путь вместо записи в реальный data/ проекта.
_BRIEF_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "brief_gen_state.json"
)

# Час отправки дайджеста (локальное время), ДО первого слота Стража (08:xx)
_DIGEST_HOUR = 8


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
        logger.warning("morning_digest: account id недоступен — %s", type(exc).__name__)
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
    """Создаёт поле и соответствующий FactClaim одной операцией.

    Отсутствующее значение (value=None) не бывает required — требовать
    подтверждения у несуществующего числа бессмысленно, а blocking-претензия
    по нему снесла бы весь отчёт вместо того, чтобы честно скрыть одно поле
    (та же конвенция, что в evening_report._append_fact). Чекер не
    ослабляется: неподтверждённое поле всё так же не рендерится.
    """
    required = required and value is not None
    fields.append(
        ReportField(
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
    )
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


def _confirmed_history(window: TimeWindow) -> tuple[int | None, str]:
    """Возвращает только подтверждённые действия и явный статус пробелов WAL."""
    try:
        from services.approval_audit import read_action_history

        history = read_action_history(window)
    except Exception as exc:
        logger.warning(
            "morning_digest: approval history недоступна — %s", type(exc).__name__
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


def _yesterday_bounds(now: datetime) -> tuple[datetime, datetime, str]:
    """Границы «вчера» в локальной TZ: [00:00:00, 23:59:59] + строка YYYY-MM-DD."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_LOCAL)
    yesterday_date = (now - timedelta(days=1)).date()
    start = datetime.combine(yesterday_date, datetime.min.time(), tzinfo=_TZ_LOCAL)
    end = start.replace(hour=23, minute=59, second=59)
    return start, end, yesterday_date.isoformat()


def _get_fb_spend_for_day(day_iso: str) -> float | None:
    """Прямой FB account-level запрос расхода ЗА ОДИН КОНКРЕТНЫЙ ДЕНЬ.

    Возвращает None, когда FB ещё НЕ отдал строку за этот день (пустой
    data[]) — это «данных ещё нет», а НЕ «расход $0». Раньше здесь стоял
    return 0.0, и вечерний отчёт в 21:00 (день кабинета открылся всего
    9 часов назад) утверждал «расход сегодня $0» на пустом ответе Graph;
    Approval Checker справедливо бракует такое утверждение кодом
    NULL_COERCED_TO_ZERO и блокирует весь отчёт. Тот же паттерн уже
    применён к Google-расходу (services/google_spend.get_daily_google_spend
    отдаёт None вместо 0 при отсутствии вкладки дня).

    НЕ используем budget_scaler.get_fb_week_spend здесь: та функция сначала
    пробует data/analytics_cache.json, а кеш хранит АГРЕГАТ за весь диапазон,
    за который его прогрели (обычно today-7..today, см. web/app.py
    _cron_prewarm). Проверка покрытия диапазона в get_fb_week_spend
    (cache_from <= date_from and cache_to >= date_to) успешно проходит для
    однодневного окна «вчера», но возвращает сумму spend за ВСЕ 7 дней —
    расход завышается в разы, хотя подписан как «Вчера» (пример: $7000 за
    неделю вместо $1000 за день). Поэтому здесь всегда идём
    напрямую в FB insights за day_iso..day_iso, без диапазонного кеша.
    """
    import json as _json

    from agent.fb_common import _throttled_get, API
    from services.fb_token_provider import get_fb_token, get_fb_account_id

    resp = _throttled_get(
        f"{API}/act_{get_fb_account_id()}/insights",
        params={
            "access_token": get_fb_token(),
            "level": "account",
            "fields": "spend",
            "time_range": _json.dumps({"since": day_iso, "until": day_iso}),
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"FB insights вернул {resp.status_code}: {resp.text[:100]}")
    rows = resp.json().get("data", [])
    if not rows:
        # Пустой data[] = FB ещё не посчитал день. Не 0 — «данных ещё нет».
        return None
    return float(rows[0].get("spend", 0) or 0)


def _collect_yesterday_metrics(now: datetime) -> dict[str, object]:
    """Собирает исходные значения секции, не превращая ошибку в ноль."""
    y_start, y_end, y_str = _yesterday_bounds(now)
    metrics: dict[str, object] = {
        "window_start": y_start,
        "window_end": y_end + timedelta(seconds=1),
        "date": y_str,
        "fb_spend": None,
        "google_spend": None,
        "google_last_known": None,
        "leads": None,
        "quals": None,
        "payments_amo_status": None,
    }
    try:
        from services.google_spend import (
            get_daily_google_spend,
            get_last_known_google_spend,
        )

        metrics["fb_spend"] = _get_fb_spend_for_day(y_str)
        metrics["google_spend"] = get_daily_google_spend(y_str)
        if metrics["google_spend"] is None:
            metrics["google_last_known"] = get_last_known_google_spend(y_str)
    except Exception as exc:
        logger.warning("morning_digest: расход вчера недоступен — %s", exc)

    try:
        from integrations.amo import get_leads_window, classify_lead

        leads = get_leads_window(int(y_start.timestamp()), int(y_end.timestamp()))
        metrics["leads"] = len(leads)
        metrics["quals"] = sum(1 for lead in leads if classify_lead(lead) == "квал")
        # Статус AMO не является платёжным доказательством. Значение сохраняем
        # только для legacy-текста и никогда не включаем в checked payload.
        metrics["payments_amo_status"] = sum(
            1 for lead in leads if classify_lead(lead) == "оплата"
        )
    except Exception as exc:
        logger.warning("morning_digest: лиды/квалы/оплаты недоступны — %s", exc)
    return metrics


def _section_yesterday(
    now: datetime,
    metrics: dict[str, object] | None = None,
) -> list[str]:
    """Секция «Вчера»: расход FB+Google / лиды / квалы / оплаты.

    Расход FB — прямой FB-запрос за конкретный день (_get_fb_spend_for_day),
    НЕ через budget_scaler.get_fb_week_spend — та читает многодневный
    analytics_cache.json и для однодневного окна возвращает завышенную сумму
    (см. docstring _get_fb_spend_for_day). Google — через
    budget_scaler.get_google_week_spend (там per-day история снимков,
    диапазон в 1 день суммирует честно, баг не относится к ней).
    Лиды/квалы/оплаты — один вызов integrations.amo.get_leads_window за вчера,
    classify_lead по каждому лиду.
    """
    metrics = metrics or _collect_yesterday_metrics(now)

    # --- Расход FB+Google ---
    spend_str = "нет данных"
    try:
        from services.formatting import fmt_money

        fb_spend = metrics["fb_spend"]
        if fb_spend is None:
            raise ValueError("FB spend unavailable")
        # get_daily_google_spend различает «0» и «нет данных»: None = вкладку дня
        # подрядчик ещё не залил (раньше отдавалось как «$0» — исправлено),
        # число (в т.ч. 0.0) = снимок дня есть.
        google_spend = metrics["google_spend"]
        if google_spend is None:
            last_known = metrics["google_last_known"]
            if last_known:
                last_iso, last_val = last_known
                last_dd_mm = date.fromisoformat(last_iso).strftime("%d.%m")
                google_note = (
                    f"Google: данных ещё нет · последний известный день "
                    f"{last_dd_mm}: {fmt_money(last_val, '$')}"
                )
            else:
                google_note = "Google: данных ещё нет"
            spend_str = f"FB ${fb_spend:.0f} · {google_note}"
        else:
            total_spend = float(fb_spend) + float(google_spend)
            spend_str = (
                f"${total_spend:.0f} (FB ${fb_spend:.0f} + Google ${google_spend:.0f})"
            )
    except Exception as exc:
        logger.warning("morning_digest: расход вчера недоступен — %s", exc)

    # --- Лиды/квалы/оплаты из AMO ---
    leads_str = "нет данных"
    try:
        total_leads = metrics["leads"]
        quals = metrics["quals"]
        payments = metrics["payments_amo_status"]
        if not all(isinstance(value, int) for value in (total_leads, quals, payments)):
            raise ValueError("AMO outcomes unavailable")
        leads_str = f"лиды {total_leads} · квалы {quals} · оплаты {payments}"
    except Exception as exc:
        logger.warning("morning_digest: лиды/квалы/оплаты недоступны — %s", exc)
        leads_str = "лиды/квалы/оплаты: нет данных"

    return [f"Вчера: расход {spend_str} · {leads_str}"]


def _section_unit_economics(now: datetime) -> list[str]:
    """Секция «Юнитка»: факт ДРР (trailing-окно до субботы) vs план unit_target.

    ДРР считаем ТОЧНО как budget_scaler._run_scaling_inner (§8.2 спеки):
    compute_drr_window → get_fb_week_spend + get_google_week_spend (окно ДРР) +
    get_amo_week_revenue (окно ДРР) → actual_drr = spend_usd*rate / revenue_lcy.
    План unit_target — из plan_reader.read_general_plan(sheet_id). sheet_id пуст → «план: н/д».
    """
    try:
        from services.budget_scaler import (
            compute_drr_window,
            get_fb_week_spend,
            get_google_week_spend,
            get_amo_week_revenue,
        )
        from services.budget_scaler import get_scale_config

        cfg = get_scale_config()
        sheet_id = cfg.get("plan_sheet_id", "")
        if not sheet_id:
            return ["Юнитка: план н/д (не задан sheet_id)"]

        from services.plan_reader import read_general_plan

        plan_data = read_general_plan(sheet_id, now=now)
        if not plan_data:
            return ["Юнитка: план н/д (ошибка чтения плана)"]

        unit_target = plan_data.get("unit_target", 0.0)

        drr_start, drr_end, last_saturday = compute_drr_window(now)
        drr_from = drr_start.strftime("%Y-%m-%d")
        drr_to = drr_end.strftime("%Y-%m-%d")

        fb_drr = get_fb_week_spend(drr_from, drr_to)
        google_drr = get_google_week_spend(drr_from, drr_to)
        revenue_lcy = get_amo_week_revenue(drr_start, drr_end)

        window_label = last_saturday.strftime("%d.%m")

        if revenue_lcy <= 0:
            return [
                f"Юнитка: факт н/д (выручка ¤0 за окно до сб {window_label}) "
                f"/ план {unit_target * 100:.1f}%"
            ]

        try:
            from services.exchange_rate import get_usd_to_lcy

            usd_lcy_rate = get_usd_to_lcy()
        except Exception:
            import config as _config

            usd_lcy_rate = float(_config.USD_TO_LCY)  # fallback — как в budget_scaler

        total_spend_usd = fb_drr + google_drr
        actual_drr = (total_spend_usd * usd_lcy_rate) / revenue_lcy

        warn = " ⚠️" if actual_drr > unit_target else ""
        return [
            f"Юнитка: факт {actual_drr * 100:.0f}% / план {unit_target * 100:.0f}%{warn} "
            f"(ДРР за окно до сб {window_label})"
        ]
    except Exception as exc:
        logger.warning("morning_digest: юнитка недоступна — %s", exc)
        return ["Юнитка: нет данных"]


# State авто-запуска (пишет services/auto_launch.py). Модульная константа —
# чтобы тесты могли подменить путь, как _BRIEF_STATE_PATH.
_AUTO_LAUNCH_STATE_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "auto_launch_state.json"
)
# В дайджесте показываем не больше 5 отказов — остальное «… и ещё K»
# (лимит дайджеста — 20 непустых строк).
_BLOCKED_DIGEST_MAX_LINES = 5
_BLOCKED_DIGEST_REASON_LIMIT = 120


def _plural_cards(n: int) -> str:
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return "карточку"
    if 2 <= n_abs % 10 <= 4 and not 12 <= n_abs % 100 <= 14:
        return "карточки"
    return "карточек"


def _section_auto_launch_blocked(now: datetime) -> list[str]:
    """Секция «Автозапуск не пропустил»: карточки, которые последний прогон
    авто-запуска отклонил, с кодом проверки и первой причиной.

    Читает last_run_blocked/last_run_at из data/auto_launch_state.json —
    их пишет services/auto_launch.py::_record_last_run_blocked на КАЖДОМ
    завершении прогона (dry_run и active). Прогон старше суток не показываем:
    иначе одна и та же стена отказов висела бы в дайджесте каждое утро, пока
    бот стоит. Пусто или файла нет → секция опускается целиком (нет данных —
    не врём). Ошибки — наружу, их ловит build_morning_digest.
    """
    import json

    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_LOCAL)
    if not _AUTO_LAUNCH_STATE_PATH.exists():
        return []
    state = json.loads(_AUTO_LAUNCH_STATE_PATH.read_text(encoding="utf-8"))
    blocked = [entry for entry in (state.get("last_run_blocked") or []) if isinstance(entry, dict)]
    if not blocked:
        return []

    run_label = ""
    run_at_raw = state.get("last_run_at")
    if run_at_raw:
        try:
            run_at = datetime.fromisoformat(str(run_at_raw))
        except (ValueError, TypeError):
            run_at = None
        if run_at is not None:
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=_TZ_LOCAL)
            if run_at < now - timedelta(hours=24):
                return []
            run_label = f" (прогон {run_at.astimezone(_TZ_LOCAL).strftime('%d.%m %H:%M')})"

    lines = [
        f"🚫 Автозапуск не пропустил {len(blocked)} {_plural_cards(len(blocked))}{run_label}:"
    ]
    for entry in blocked[:_BLOCKED_DIGEST_MAX_LINES]:
        name = str(entry.get("card_name") or entry.get("card_id") or "без названия")
        codes = ", ".join(
            str(code) for code in (entry.get("reason_codes") or []) if str(code)
        ) or "LAUNCH_CHECK_BLOCKED"
        reason = " ".join(str(entry.get("reason") or "причина не указана").split())
        if len(reason) > _BLOCKED_DIGEST_REASON_LIMIT:
            reason = reason[: _BLOCKED_DIGEST_REASON_LIMIT - 1].rstrip() + "…"
        lines.append(
            f"• {html.escape(name)} — {html.escape(codes)}: {html.escape(reason)}"
        )
    rest = len(blocked) - _BLOCKED_DIGEST_MAX_LINES
    if rest > 0:
        lines.append(f"… и ещё {rest}")
    return lines


def _section_actions_24h(now: datetime) -> list[str]:
    """Секция «Ночью/вчера сделал»: агрегат решений автопилота за последние 24ч.

    decisions_repo.get_decisions_history(date_from=now-24ч, date_to=now), фильтр
    confirmed_by in _AUTOPILOT_CONFIRMED_BY (как в evening_report), агрегируем по action.
    Запуски и карточки ТЗ НЕ пишутся в decisions_repo (проверено по коду auto_launch.py
    и brief_generator.py — свои state-файлы) — читаем их отдельно, каждый в своём try.
    """
    lines = ["Ночью/вчера сделал:"]

    paused = raised = cleaned = 0
    try:
        import importlib
        import sys

        importlib.import_module("agent.repositories.decisions_repo")
        decisions_repo = sys.modules["agent.repositories.decisions_repo"]

        date_from = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        date_to = now.strftime("%Y-%m-%d %H:%M:%S")

        result = decisions_repo.get_decisions_history(
            tenant_id="default",
            date_from=date_from,
            date_to=date_to,
            limit=500,
        )
        decisions = result.get("decisions", [])
        autopilot_decisions = [
            d for d in decisions if d.get("confirmed_by") in _AUTOPILOT_CONFIRMED_BY
        ]

        for d in autopilot_decisions:
            action = d.get("action", "")
            if action == "PAUSED":
                paused += 1
            elif action == "BUDGET_RAISED":
                raised += 1
            elif action == "DELETED_STALE":
                cleaned += 1
    except Exception as exc:
        logger.warning("morning_digest: решения за 24ч недоступны — %s", exc)

    # --- Запуски за сутки: auto_launch_state.json, launched_ever со временем ---
    launches = 0
    try:
        import json

        state_path = _AUTO_LAUNCH_STATE_PATH
        if state_path.exists():
            from services.auto_launch import _launched_at

            state = json.loads(state_path.read_text(encoding="utf-8"))
            launched_ever = state.get("launched_ever", {})
            cutoff = now - timedelta(hours=24)
            # _launched_at читает дату из ОБОИХ форматов записи (старый — строка
            # iso_datetime; новый — {"at":..., "name":...}) — не ломаем чтение
            # старых записей без имени карточки.
            for entry in launched_ever.values():
                iso_ts = _launched_at(entry)
                try:
                    ts = datetime.fromisoformat(iso_ts)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_TZ_LOCAL)
                    if ts >= cutoff:
                        launches += 1
                except (ValueError, TypeError):
                    continue
    except Exception as exc:
        logger.warning("morning_digest: запуски за 24ч недоступны — %s", exc)

    # --- Карточки ТЗ за сутки: brief_gen_state.json, последний прогон в сутках ---
    # Файл не хранит per-card timestamp, только метку последнего запуска —
    # приблизительно считаем «сделал за сутки», если генератор реально
    # запускался (плановый ИЛИ ручной) в последние 24ч.
    # Регрессия: раньше читали единый last_run_date, теперь
    # брифогенератор пишет last_scheduled_run_date/last_manual_run_date
    # раздельно (см. services/brief_generator.py) — _latest_brief_run_date
    # берёт более свежую из двух меток, fallback на legacy last_run_date.
    briefs = 0
    try:
        import json

        from services.brief_generator import _latest_brief_run_date

        if _BRIEF_STATE_PATH.exists():
            state = json.loads(_BRIEF_STATE_PATH.read_text(encoding="utf-8"))
            last_run_date = _latest_brief_run_date(state)
            if last_run_date:
                today_str = now.date().isoformat()
                yesterday_str = (now - timedelta(days=1)).date().isoformat()
                # last_run_date — полный ISO-таймстамп (datetime.now().isoformat()),
                # сравниваем только дату (первые 10 символов), не строку целиком.
                if last_run_date[:10] in (today_str, yesterday_str):
                    briefs = len(state.get("generated_signatures", [])[-3:])
    except Exception as exc:
        logger.warning("morning_digest: карточки ТЗ за 24ч недоступны — %s", exc)

    lines.append(
        f"⏸ {paused} паузы · ⬆️ {raised} подъём бюджета · 🧹 {cleaned} чисток · "
        f"🚀 {launches} запуск · 🃏 {briefs} карточки ТЗ"
    )
    return lines


def _section_today_plan() -> list[str]:
    """Секция «Сегодня по плану»: слоты Стража, авто-запуск, скейлер.

    Статический текст — расписание кронов фиксировано (см. §7.2 спеки):
    Страж 08-22 каждые 2ч, авто-запуск 10:00, масштабирование бюджетов 13:00.
    """
    return [
        "Сегодня по плану:",
        "Слоты Стража 08–22 · авто-запуск 10:00 · масштабирование 13:00",
    ]


def _section_launch_verify(now: datetime) -> list[str]:
    """Секция «Запуски вчера»: сколько объявлений реально крутилось из запущенных.

    Читает data/launch_verify_state.json (пишет services/launch_verify.py крон
    _cron_launch_verify, реальный прогон 14:xx по локальному времени). Файл хранит результат
    ПОСЛЕДНЕГО прогона — если он датирован вчера, показываем строку; если
    вчера прогона не было (например бот только что включили, или вчера
    запусков не случилось — verify тихо скипает и state не трогает) — строка
    пропускается целиком (нет данных = не врём цифрой).
    """
    from services.launch_verify import load_verify_state

    state = load_verify_state()
    if not state:
        return []

    _, _, yesterday_str = _yesterday_bounds(now)
    if state.get("date") != yesterday_str:
        return []

    launched = state.get("launched", 0)
    running = state.get("running", 0)
    return [f"Запуски вчера: крутятся {running}/{launched}"]


def _section_anomalies(now: datetime) -> list[str]:
    """Секция «Аномалии»: читает только CPL и FB-error детекторы.

    Вызываем сами detect_* функции напрямую (не run_anomaly_alerts — тот шлёт алерты
    в health-канал, дайджест должен только ЧИТАТЬ, не дублировать отправку).
    Каждый детектор изолирован: сбой одного не скрывает результат второго.
    Секция опускается целиком, если детекторы ничего не нашли (экономим строки).
    """
    try:
        from services import anomaly_alerts
    except Exception as exc:
        logger.debug("morning_digest: модуль аномалий недоступен — %s", exc)
        return []

    alerts: list[str] = []
    try:
        alerts.extend(anomaly_alerts.detect_cpl_spike_by_city(now))
    except Exception as exc:
        logger.debug("morning_digest: CPL-детектор недоступен — %s", exc)

    try:
        alerts.extend(anomaly_alerts.detect_fb_error_burst(now))
    except Exception as exc:
        logger.debug("morning_digest: FB-error детектор недоступен — %s", exc)

    if not alerts:
        return []

    lines = ["⚠️ Аномалии:"]
    lines.extend(html.escape(text) for text in alerts)
    return lines


def build_morning_digest_request(
    now: datetime,
    yesterday_metrics: dict[str, object],
) -> ReportCheckRequest:
    """Строит typed-манифест утреннего отчёта и CONFIRMED-only WAL history."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_LOCAL)
    else:
        now = now.astimezone(_TZ_LOCAL)
    account = _account_subject()
    checker = SubjectRef(SubjectKind.CHECKER, "approval-action-history")
    yesterday_window = TimeWindow(
        start=yesterday_metrics["window_start"],
        end=yesterday_metrics["window_end"],
        timezone_name="Etc/GMT-5",
        semantic="closed_local_day",
    )
    history_window = TimeWindow(
        start=now - timedelta(hours=24),
        end=now,
        timezone_name="Etc/GMT-5",
        semantic="confirmed_actions_last_24h",
    )
    fields: list[ReportField] = []
    claims: list[FactClaim] = []
    section_fields = {
        "facebook_yesterday": [],
        "outcomes_yesterday": [],
        "actions_24h": [],
    }

    _append_fact(
        fields,
        claims,
        section_fields,
        field_id="yesterday.window_start",
        section_id="facebook_yesterday",
        category=FactCategory.WINDOW_BOUND,
        label=FieldLabelTemplate.WINDOW_START,
        field_format=FieldFormat.DATETIME,
        subject=account,
        metric=Metric.WINDOW_START,
        value=yesterday_window.start.isoformat(),
        source=SourceSystem.FACEBOOK,
        window=yesterday_window,
    )
    _append_fact(
        fields,
        claims,
        section_fields,
        field_id="yesterday.window_end",
        section_id="facebook_yesterday",
        category=FactCategory.WINDOW_BOUND,
        label=FieldLabelTemplate.WINDOW_END,
        field_format=FieldFormat.DATETIME,
        subject=account,
        metric=Metric.WINDOW_END,
        value=yesterday_window.end.isoformat(),
        source=SourceSystem.FACEBOOK,
        window=yesterday_window,
    )
    _append_fact(
        fields,
        claims,
        section_fields,
        field_id="yesterday.fb_spend",
        section_id="facebook_yesterday",
        category=FactCategory.BUSINESS_METRIC,
        label=FieldLabelTemplate.SPEND,
        field_format=FieldFormat.USD,
        subject=account,
        metric=Metric.SPEND,
        value=_as_decimal(yesterday_metrics.get("fb_spend")),
        source=SourceSystem.FACEBOOK,
        window=yesterday_window,
        currency="USD",
    )
    _append_fact(
        fields,
        claims,
        section_fields,
        field_id="yesterday.amo_leads",
        section_id="outcomes_yesterday",
        category=FactCategory.BUSINESS_METRIC,
        label=FieldLabelTemplate.LEADS,
        field_format=FieldFormat.INTEGER,
        subject=account,
        metric=Metric.LEADS,
        value=yesterday_metrics.get("leads"),
        source=SourceSystem.AMO,
        window=yesterday_window,
    )
    _append_fact(
        fields,
        claims,
        section_fields,
        field_id="yesterday.amo_quals",
        section_id="outcomes_yesterday",
        category=FactCategory.BUSINESS_METRIC,
        label=FieldLabelTemplate.QUALS,
        field_format=FieldFormat.INTEGER,
        subject=account,
        metric=Metric.QUALS,
        value=yesterday_metrics.get("quals"),
        source=SourceSystem.AMO,
        window=yesterday_window,
    )

    confirmed_count, history_state = _confirmed_history(history_window)
    _append_fact(
        fields,
        claims,
        section_fields,
        field_id="actions.confirmed_count",
        section_id="actions_24h",
        category=FactCategory.HISTORY_AUDIT,
        label=FieldLabelTemplate.ACTION_COUNT,
        field_format=FieldFormat.INTEGER,
        subject=checker,
        metric=Metric.ACTION_COUNT,
        value=confirmed_count,
        source=SourceSystem.CHECKER_AUDIT,
        window=history_window,
    )
    _append_fact(
        fields,
        claims,
        section_fields,
        field_id="actions.history_state",
        section_id="actions_24h",
        category=FactCategory.HISTORY_AUDIT,
        label=FieldLabelTemplate.HISTORY_STATE,
        field_format=FieldFormat.STATUS,
        subject=checker,
        metric=Metric.HISTORY_STATE,
        value=history_state,
        source=SourceSystem.CHECKER_AUDIT,
        window=history_window,
    )

    sections = (
        ReportSection(
            "facebook_yesterday",
            SectionTemplate.FACEBOOK,
            tuple(section_fields["facebook_yesterday"]),
            yesterday_window,
        ),
        ReportSection(
            "outcomes_yesterday",
            SectionTemplate.OUTCOMES,
            tuple(section_fields["outcomes_yesterday"]),
            yesterday_window,
        ),
        ReportSection(
            "actions_24h",
            SectionTemplate.ACTIONS,
            tuple(section_fields["actions_24h"]),
            history_window,
        ),
    )
    payload = TypedReportPayload(
        template=ReportTemplate.MORNING,
        sections=sections,
        fields=tuple(fields),
        generated_at=now,
        mixed_windows_explicit=True,
    )
    claims_tuple = tuple(claims)
    return ReportCheckRequest(
        correlation_id=f"morning:{now.date().isoformat()}:{uuid.uuid4()}",
        payload=payload,
        claims=claims_tuple,
        manifest_sha256=report_manifest_sha256(payload, claims_tuple),
    )


def build_morning_digest(now: datetime | None = None) -> dict:
    """Строит утренний дайджест — {"text": html_str}.

    Никогда не бросает исключений: каждая секция в своём try, сбой секции
    заменяется на заглушку «нет данных» (кроме «Аномалии» — та секция
    опускается целиком при отсутствии аномалий/ошибке, чтобы не засорять
    дайджест лишней строкой).
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_LOCAL)

    date_str = now.strftime("%d.%m")
    lines = [f"🌅 <b>Утро {date_str}</b>", ""]

    yesterday_metrics = _collect_yesterday_metrics(now)
    try:
        lines.extend(_section_yesterday(now, yesterday_metrics))
    except Exception as exc:
        logger.warning("morning_digest: секция «Вчера» упала — %s", exc)
        lines.append("Вчера: нет данных")

    try:
        lines.extend(_section_unit_economics(now))
    except Exception as exc:
        logger.warning("morning_digest: секция «Юнитка» упала — %s", exc)
        lines.append("Юнитка: нет данных")

    lines.append("")

    try:
        lines.extend(_section_actions_24h(now))
    except Exception as exc:
        logger.warning("morning_digest: секция «Сделал» упала — %s", exc)
        lines.append("Ночью/вчера сделал: нет данных")

    try:
        lines.extend(_section_auto_launch_blocked(now))
    except Exception as exc:
        logger.warning("morning_digest: секция «Автозапуск не пропустил» упала — %s", exc)
        # Нет данных — строки не пишем (не врём), как и в «Запуски вчера».

    try:
        launch_verify_lines = _section_launch_verify(now)
        lines.extend(launch_verify_lines)
    except Exception as exc:
        logger.warning("morning_digest: секция «Запуски вчера» упала — %s", exc)
        # Нет данных — строку не пишем (не врём цифрой), как договорено в спеке.

    lines.append("")

    try:
        lines.extend(_section_today_plan())
    except Exception as exc:
        logger.warning("morning_digest: секция «Сегодня по плану» упала — %s", exc)
        lines.append("Сегодня по плану: нет данных")

    try:
        anomaly_lines = _section_anomalies(now)
        if anomaly_lines:
            lines.append("")
            lines.extend(anomaly_lines)
    except Exception as exc:
        logger.warning("morning_digest: секция «Аномалии» упала — %s", exc)

    text = "\n".join(lines)
    check_request = build_morning_digest_request(now, yesterday_metrics)
    return {
        "text": text,
        "payload": check_request.payload,
        "claims": check_request.claims,
        "check_request": check_request,
    }


def send_morning_digest() -> bool:
    """Проверяет typed facts и отправляет только deterministic checked render."""
    try:
        digest = build_morning_digest()
        from services.approval_checker import check_report
        from services.approval_report import render_checked_report
        from services.approval_telegram import send_checked_report, send_fact_free
        from services.approval_checker_models import FactFreeTemplate

        request = digest["check_request"]
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
        logger.warning("morning_digest: ошибка при отправке дайджеста — %s", exc)
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
                "morning_digest: safe facade недоступен — %s",
                type(fallback_exc).__name__,
            )
            return False


def should_send_digest(now: datetime, last_digest_date: date | None) -> bool:
    """Чистая функция: нужно ли слать дайджест прямо сейчас.

    Условия: час == 8 по локальному времени И today != last_digest_date (паттерн
    evening_report.should_send_report — гейт живёт в web/app.py, эта функция
    только вычисляет решение по переданным данным).

    Args:
        now: текущее время (с tzinfo или naive — naive считаем уже локальным)
        last_digest_date: date последней отправки (или None)
    """
    if now.tzinfo is not None:
        now_local = now.astimezone(_TZ_LOCAL)
    else:
        now_local = now

    if now_local.hour != _DIGEST_HOUR:
        return False

    today = now_local.date()
    if last_digest_date is not None and last_digest_date == today:
        return False

    return True
