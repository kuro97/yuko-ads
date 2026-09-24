#!/usr/bin/env python3
"""Ручная сверка галочек Trello с живой рекламой (dry-run по умолчанию).

Тот же проход, что и часовой крон ``_cron_trello_check_reconcile`` в
web/app.py, но с печатью плана: какие карточки получат галочку и по каким
объявлениям, что осталось без карточки, где имя неоднозначно. Без ``--apply``
ничего не меняет.

Пример:
    ./venv/bin/python scripts/reconcile_trello_checks.py
    ./venv/bin/python scripts/reconcile_trello_checks.py --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.trello_check_reconciler import (  # noqa: E402
    DEFAULT_LIST_NAMES,
    reconcile_trello_checks_from_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true", help="Поставить галочки по-настоящему"
    )
    parser.add_argument(
        "--lists",
        default=",".join(DEFAULT_LIST_NAMES),
        help="Колонки через запятую, где галочка = «запущено»",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    list_names = tuple(name.strip() for name in args.lists.split(",") if name.strip())

    run = reconcile_trello_checks_from_config(apply=args.apply, list_names=list_names)

    print(
        f"Кабинеты прочитаны: {', '.join('act_' + a for a in run.accounts_scanned) or '-'}; "
        f"не прочитаны: {', '.join('act_' + a for a in run.accounts_failed) or '-'}"
    )
    print(f"Колонки: {', '.join(list_names)}")

    print(f"\nКарточки без галочки с живой рекламой: {len(run.to_mark)}")
    for match in run.to_mark:
        print(f"  • [{match.card.list_name}] {match.card.name}")
        for name in match.ad_names:
            print(f"      ↳ {name}")

    if run.ambiguous:
        print(f"\nНеоднозначные имена (галочку не ставлю): {len(run.ambiguous)}")
        for match in run.ambiguous:
            print(f"  • [{match.card.list_name}] {match.card.name}")
    if run.needs_review:
        print(f"\nСлабое совпадение, одна метка через усечение — подтвердите руками: {len(run.needs_review)}")
        for match in run.needs_review:
            print(f"  • [{match.card.list_name}] {match.card.name}")
            for name in match.ad_names:
                print(f"      ↳ {name}")
    if run.outside_lists:
        print(f"\nЖивая реклама у карточек вне целевых колонок: {len(run.outside_lists)}")
        for match in run.outside_lists:
            print(f"  • [{match.card.list_name}] {match.card.name}")

    unmatched_names = sorted(dict.fromkeys(ad.name for ad in run.unmatched_ads))
    print(
        f"\nЖивых объявлений без карточки: {len(run.unmatched_ads)} "
        f"({len(unmatched_names)} имён)"
    )
    for name in unmatched_names:
        print(f"  • {name}")
    print(f"\nКарточек без галочки и без живой рекламы: {run.unchecked_without_ads}")

    if not args.apply:
        print(
            f"\nDry-run: ничего не изменено (отметилось бы {len(run.to_mark)}). "
            "Для реальной отметки добавьте --apply."
        )
        return 0
    print(f"\nОтмечено: {len(run.marked)}, не удалось: {len(run.failed)}")
    for card_id in run.failed:
        print(f"  ✗ {card_id}")
    return 1 if run.failed or run.accounts_failed else 0


if __name__ == "__main__":
    sys.exit(main())
