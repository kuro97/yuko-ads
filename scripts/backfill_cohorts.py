#!/usr/bin/env python3
"""Исторический бэкфилл недельных когорт объявлений: dry-run по умолчанию.

Зачем: таблица ad_weekly_cohorts заполняется кроном только по свежим неделям.
Историю (полгода и глубже) нужно донести разово — иначе тренду не с чем
сравнивать, и первая же неделя выглядит «новой звездой».

Источник расхода — Facebook напрямую (асинхронные отчёты уровня объявления с
разбивкой по дням). Таблица ad_daily_metrics для истории НЕПРИГОДНА: дыры
почти в половину дней и legacy-семантика лидов v1.

Что делает: считает недели за указанный диапазон и делает UPSERT в
ad_weekly_cohorts. Идемпотентен — повторный запуск на том же диапазоне
перезаписывает те же строки, дублей не появляется. Полная строка
(comparable=1) неполной не затирается.

Чего НЕ делает: ничего не запускает, не паузит и не меняет бюджеты. Только
чтение FB/AMO и запись в свою таблицу.

Примеры:
    python3 scripts/backfill_cohorts.py --since 2026-02-01 --until 2026-07-26
    python3 scripts/backfill_cohorts.py --weeks 26
    python3 scripts/backfill_cohorts.py --since 2026-02-01 --until 2026-07-26 \\
        --apply --confirm-production BACKFILL-AD-WEEKLY-COHORTS
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import creative_intelligence  # noqa: E402
from services.cohort_builder import (  # noqa: E402
    REVENUE_HORIZON_DAYS,
    CohortBuildError,
    CohortRateLimitError,
    _collect_amo,
    _collect_fb,
    _collect_revenue,
    _build_rows,
    build_cohorts,
    week_start_of,
)


CONFIRM_PRODUCTION = "BACKFILL-AD-WEEKLY-COHORTS"
_TZ_LOCAL = timezone(timedelta(hours=5))
# Сколько недель истории считать по умолчанию, если диапазон не задан явно.
_DEFAULT_WEEKS = 26


def resolve_db_path(explicit: str) -> Path:
    """Тот же порядок, что у остальных операционных скриптов, без секретов."""
    if explicit:
        return Path(explicit).expanduser()
    for name in ("CREATIVE_KB_PATH", "OWNER_ACTION_DB_PATH"):
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value).expanduser()
    return ROOT / "data" / "decisions.db"


def resolve_range(args: argparse.Namespace) -> tuple[date, date]:
    """Явные --since/--until важнее --weeks. Границы выравниваются на неделю."""
    if args.since and args.until:
        since = date.fromisoformat(args.since)
        until = date.fromisoformat(args.until)
    else:
        today = datetime.now(_TZ_LOCAL).date()
        # Последняя закрытая неделя — та, чьё воскресенье уже позади.
        last_closed_end = week_start_of(today) - timedelta(days=1)
        until = last_closed_end
        since = week_start_of(last_closed_end) - timedelta(
            days=7 * (args.weeks - 1)
        )
    if since > until:
        raise SystemExit(f"Пустой диапазон: {since} > {until}")
    return since, until


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Исторический бэкфилл ad_weekly_cohorts (dry-run по умолчанию)",
    )
    parser.add_argument("--since", default="", help="ISO-дата начала, включительно")
    parser.add_argument("--until", default="", help="ISO-дата конца, включительно")
    parser.add_argument(
        "--weeks", type=int, default=_DEFAULT_WEEKS,
        help="сколько закрытых недель назад считать, если --since/--until не заданы",
    )
    parser.add_argument("--db-path", default="")
    parser.add_argument("--apply", action="store_true", help="писать в БД")
    parser.add_argument(
        "--confirm-production", default="",
        help=f"обязателен вместе с --apply, значение: {CONFIRM_PRODUCTION}",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def _print_summary(summary: dict, *, title: str) -> None:
    print(title)
    print(f"  диапазон:        {summary['since']} .. {summary['until']}")
    print(f"  недель:          {summary['weeks']}")
    print(f"  строк когорт:    {summary['rows']}")
    print(f"  сравнимых:       {summary['comparable']}")
    print(f"  несравнимых:     {summary['not_comparable']}")
    reasons = summary.get("reasons") or {}
    if reasons:
        print("  причины несравнимости:")
        for code, count in sorted(reasons.items(), key=lambda item: -item[1]):
            print(f"    {code}: {count}")
    else:
        print("  причины несравнимости: —")
    print(
        f"  дней FB покрыто: {summary['fb_days_covered']} из "
        f"{summary['fb_days_expected']}"
    )
    print(
        f"  лидов AMO:       {summary['amo_leads_total']} "
        f"(с разметкой {summary['amo_leads_marked']}, "
        f"{summary['amo_marked_share_pct']}%; "
        f"без разметки {summary['amo_leads_unmarked']})"
    )
    if summary.get("revenue_source_ok"):
        print(
            f"  выручка (гориз. {summary['revenue_horizon_days']} дн): "
            f"дозревших недель {summary['weeks_revenue_mature']}, "
            f"строк с деньгами {summary['rows_with_revenue']}, "
            f"с ROMI {summary['rows_with_romi']}"
        )
        print(
            f"  платежей ERP:     {summary['payments_total']} "
            f"(сопоставлено {summary['payments_matched']}, "
            f"по чужим лидам {summary['payments_unmatched_lead']}, "
            f"вне горизонта {summary['payments_outside_horizon']})"
        )
        if summary.get("revenue_rate_missing"):
            print(
                f"  БЕЗ КУРСА:       {summary['revenue_rate_missing']} строк — "
                f"ROMI не посчитан (по сегодняшнему курсу не считаем)"
            )
        failed = summary.get("revenue_failed_spans") or []
        if failed:
            print(
                f"  ERP НЕ ОТДАЛА:    {len(failed)} окон — недели с оплатами "
                f"в них остались NULL (не ноль):"
            )
            for span_from, span_to in failed:
                print(f"    {span_from} .. {span_to}")
    else:
        print("  выручка:         ERP недоступна — деньги остались NULL (не ноль)")
    if "written" in summary:
        print(f"  записано строк:  {summary['written']}")
        print(f"  сохранено прежних (полная строка не затирается неполной): "
              f"{summary['kept']}")


def _dry_run(since: date, until: date) -> dict:
    """Считает всё то же самое, но не пишет ни строки."""
    fb_weeks, fb_covered_days = _collect_fb(since, until)
    amo_weeks, lead_index, amo_stats = _collect_amo(since, until)
    revenue_weeks, revenue_stats = _collect_revenue(
        lead_index, since, until, REVENUE_HORIZON_DAYS
    )
    rows = _build_rows(
        since,
        until,
        fb_weeks,
        fb_covered_days,
        amo_weeks,
        None,
        revenue_weeks=revenue_weeks,
        revenue_source_ok=bool(revenue_stats["revenue_source_ok"]),
        revenue_covered_days=revenue_stats.get("revenue_covered_days"),
        horizon_days=REVENUE_HORIZON_DAYS,
    )

    reasons: dict[str, int] = {}
    for row in rows:
        if not row["not_comparable_reason"]:
            continue
        for chunk in row["not_comparable_reason"].split(";"):
            code = chunk.split(":", 1)[0]
            reasons[code] = reasons.get(code, 0) + 1
    comparable = sum(1 for row in rows if row["comparable"] == 1)
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "weeks": len({row["week_start"] for row in rows}),
        "rows": len(rows),
        "comparable": comparable,
        "not_comparable": len(rows) - comparable,
        "reasons": reasons,
        "fb_days_expected": (until - since).days + 1,
        "fb_days_covered": len(fb_covered_days),
        "revenue_horizon_days": REVENUE_HORIZON_DAYS,
        "weeks_revenue_mature": len(
            {row["week_start"] for row in rows if row["revenue_mature"] == 1}
        ),
        "rows_with_revenue": sum(
            1 for row in rows if row["revenue_lcy"] is not None
        ),
        "rows_with_romi": sum(1 for row in rows if row["romi_pct"] is not None),
        "revenue_rate_missing": sum(
            1
            for row in rows
            if row["revenue_lcy"] is not None and row["usd_lcy_rate"] is None
        ),
        **amo_stats,
        **{
            key: value
            for key, value in revenue_stats.items()
            if key != "revenue_covered_days"
        },
        "revenue_days_covered": len(revenue_stats.get("revenue_covered_days") or ()),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    since, until = resolve_range(args)
    db_path = resolve_db_path(args.db_path)

    if args.apply and args.confirm_production != CONFIRM_PRODUCTION:
        print(
            "Отказ: --apply требует --confirm-production "
            f"{CONFIRM_PRODUCTION}",
            file=sys.stderr,
        )
        return 2

    creative_intelligence.init_kb(str(db_path))
    print(f"БД: {db_path}")
    print(f"Режим: {'ЗАПИСЬ' if args.apply else 'dry-run (ничего не пишем)'}")

    try:
        if args.apply:
            summary = build_cohorts(since, until)
            _print_summary(summary, title="Когорты собраны:")
        else:
            summary = _dry_run(since, until)
            _print_summary(summary, title="Когорты посчитаны (без записи):")
    except CohortRateLimitError as exc:
        print(f"Facebook ограничил запросы, ничего не записано: {exc}", file=sys.stderr)
        return 3
    except CohortBuildError as exc:
        print(f"Данные неполные, ничего не записано: {exc}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
