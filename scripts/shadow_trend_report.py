#!/usr/bin/env python3
"""Теневой отчёт тренд-политики: что бот СКАЗАЛ БЫ по когортам.

Только чтение. Ничего не пишет ни в базу, ни в Facebook, ни в Telegram —
это диагностика перед тем, как давать автономии право действовать.

Запуск:
    venv/bin/python scripts/shadow_trend_report.py            # адсеты
    venv/bin/python scripts/shadow_trend_report.py --level ad # объявления
    venv/bin/python scripts/shadow_trend_report.py --weeks 12
"""

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.trend_policy import (  # noqa: E402
    TrendLevel,
    TrendStatus,
    evaluate_cohort_rows,
)

# Локальное время (UTC+5 по умолчанию, настраивается)
_TZ_LOCAL = timezone(timedelta(hours=5))

# Сколько недель истории отдаём политике. Ей нужно минимум три подряд, чтобы
# отличить тренд от качания, поэтому меньше шести брать смысла нет.
_DEFAULT_WEEKS = 12

# Минимум окна. Политике нужны ТРИ дозревшие недели подряд, а окно
# отсчитывается от понедельника текущей недели: одна неделя уходит на
# незакрытую, и с понедельника по субботу ещё одна — на недозревшую.
_MIN_WEEKS = 4

_STATUS_LABEL = {
    TrendStatus.GROWTH: "растёт",
    TrendStatus.DECLINE: "падает",
    TrendStatus.PLATEAU: "ровно",
    TrendStatus.INSUFFICIENT: "мало данных",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Теневой отчёт тренд-политики по когортам (только чтение)"
    )
    parser.add_argument(
        "--level",
        choices=("adset", "ad"),
        default="adset",
        help="уровень оценки: adset (по умолчанию) или ad",
    )
    parser.add_argument(
        "--weeks",
        type=int,
        default=_DEFAULT_WEEKS,
        help=(
            f"сколько последних недель подать политике "
            f"(по умолчанию {_DEFAULT_WEEKS}, минимум {_MIN_WEEKS})"
        ),
    )
    parser.add_argument("--db-path", default=None, help="путь к базе решений")
    return parser


def resolve_db_path(explicit: str | None) -> str:
    if explicit:
        return explicit
    return str(Path(__file__).resolve().parent.parent / "data" / "decisions.db")


def load_rows(db_path: str, since: date) -> list[dict]:
    """Все строки когорт от since — включая несравнимые.

    Отбирать сравнимые здесь НЕЛЬЗЯ, хотя соблазн есть. Политика различает
    «неделя дырявая» и «недели нет», и на дырявой честно отказывается судить.
    Фильтр в SQL превращает первое во второе, и получается наоборот — уверенный
    вердикт там, где данных не хватает:
      - на адсете неделя собирается из ВЫЖИВШИХ объявлений и выглядит полной,
        а падение объёма читается как падение спроса;
      - на объявлении выпавшая неделя сдвигает «сейчас» на неделю назад, и
        свежий вердикт молча строится на старых данных.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            """
            SELECT * FROM ad_weekly_cohorts
            WHERE week_start >= ?
            ORDER BY week_start, ad_id
            """,
            (since.isoformat(),),
        )
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.OperationalError as exc:
        raise SystemExit(
            f"Не читается база {db_path}: {exc}\n"
            "Проверь путь и что миграции применены (нужна таблица "
            "ad_weekly_cohorts из migrations/024)."
        ) from exc
    finally:
        conn.close()


def format_week(week) -> str:
    if week is None:
        return "—"
    qual = "—" if week.qual_pct is None else f"{week.qual_pct:.1f}%"
    leads = "—" if week.leads is None else week.leads
    return f"{week.week_start.isoformat()}: {leads} лидов, квал {qual}"


def print_verdict(verdict, indent: str = "  ") -> None:
    """Полная карточка вердикта: недели, дельта, порог, ROMI, основание.

    Печатается для КАЖДОГО вердикта, а не только для тех, кому разрешено
    действовать: на уровне объявления право не выдаётся никогда, и без цифр
    отчёт по объявлениям был бы пустым списком причин.
    """
    print(f"{indent}[{_STATUS_LABEL[verdict.status]}] "
          f"{verdict.entity_name or verdict.entity_id}")
    print(f"{indent}    было  {format_week(verdict.previous)}")
    print(f"{indent}    стало {format_week(verdict.recent)}")
    if verdict.delta_pp is not None:
        floor = verdict.noise_floor_pp
        print(
            f"{indent}    дельта {verdict.delta_pp:+.1f} п.п."
            + (f" при пороге шума {floor:.1f} п.п." if floor is not None else "")
        )
    if verdict.total_delta_pp is not None:
        print(f"{indent}    накопленная дельта {verdict.total_delta_pp:+.1f} п.п.")
    if verdict.romi_change_pp is not None:
        print(
            f"{indent}    ROMI {verdict.romi_previous:.0f}% → "
            f"{verdict.romi_recent:.0f}% (справочно)"
        )
    print(f"{indent}    основание: {verdict.reason}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.weeks < _MIN_WEEKS:
        print(
            f"Отказ: нужно минимум {_MIN_WEEKS} недели окна. Политике нужны три "
            "дозревшие недели подряд, а окно от понедельника текущей недели "
            "тратит одну на незакрытую неделю и обычно ещё одну на недозревшую."
        )
        return 2

    db_path = resolve_db_path(args.db_path)
    today = datetime.now(_TZ_LOCAL).date()
    monday = today - timedelta(days=today.weekday())
    since = monday - timedelta(weeks=args.weeks)

    rows = load_rows(db_path, since)
    if not rows:
        print(f"В базе нет когорт с {since} — сначала бэкфилл")
        return 1

    level = TrendLevel.ADSET if args.level == "adset" else TrendLevel.AD
    id_key = "adset_id" if level is TrendLevel.ADSET else "ad_id"
    # Строки без ключа группировки политика молча пропускает — считаем их сами,
    # чтобы отчёт не выглядел полнее, чем он есть.
    skipped = sum(
        1 for row in rows
        if not isinstance(row.get(id_key), str) or not row[id_key].strip()
    )
    not_comparable = sum(1 for row in rows if row.get("comparable") != 1)

    verdicts = evaluate_cohort_rows(rows, level=level, as_of=today)

    by_status: dict[TrendStatus, list] = {}
    for verdict in verdicts:
        by_status.setdefault(verdict.status, []).append(verdict)

    print(f"ТЕНЕВОЙ ОТЧЁТ ТРЕНДА — {today}, уровень {args.level}")
    print(f"  окно: {since} .. {monday}, строк когорт {len(rows)}")
    print(f"  из них несравнимых (неполная неделя): {not_comparable}")
    if skipped:
        print(f"  пропущено без {id_key}: {skipped} строк — в оценку не вошли")
    print(f"  сущностей оценено: {len(verdicts)}")
    print()

    actionable = [v for v in verdicts if v.action_grade]
    print(f"С ПРАВОМ ДЕЙСТВОВАТЬ: {len(actionable)}")
    for verdict in sorted(actionable, key=lambda v: (v.status.value, v.entity_id)):
        print_verdict(verdict)
    print()

    for status in (TrendStatus.DECLINE, TrendStatus.GROWTH, TrendStatus.PLATEAU,
                   TrendStatus.INSUFFICIENT):
        group = [v for v in by_status.get(status, []) if not v.action_grade]
        if not group:
            continue
        print(f"{_STATUS_LABEL[status].upper()} без права действовать: {len(group)}")
        for verdict in sorted(group, key=lambda v: v.entity_id):
            print_verdict(verdict)
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
