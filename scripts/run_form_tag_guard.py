#!/usr/bin/env python3
"""Запуск стража тега лид-формы (логика — services/form_tag_guard.py).

Онлайн-лиды одной и той же FB-формы получают тег формы не всегда — какая из
двух интеграций успела создать сделку, такой тег и достался. Страж дописывает
недостающий тег по полю fb_form_id, существующие теги сохраняет.

Без --apply — dry-run: только лог того, что было бы сделано, без записи в AMO.

Примеры:
    python scripts/run_form_tag_guard.py --minutes 120        # обкатка за 2 часа
    python scripts/run_form_tag_guard.py --apply              # боевой прогон крона
    python scripts/run_form_tag_guard.py --backfill --limit 500 --apply   # дотегировать историю
"""

import argparse
import logging
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from services.form_tag_guard import run  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("form_tag_guard")


def main() -> int:
    parser = argparse.ArgumentParser(description="Страж тега лид-формы AMO")
    parser.add_argument("--minutes", type=int, default=60,
                        help="окно сканирования сделок назад (по умолчанию 60)")
    parser.add_argument("--backfill", action="store_true",
                        help="искать по формам через поиск AMO, а не по окну — дотегировать историю")
    parser.add_argument("--apply", action="store_true",
                        help="реально писать в AMO (иначе dry-run)")
    parser.add_argument("--limit", type=int, default=100,
                        help="максимум тегов за прогон")
    args = parser.parse_args()

    stats = run(
        window_minutes=args.minutes,
        backfill=args.backfill,
        apply=args.apply,
        limit=args.limit,
    )
    log.info(
        "Итог: просмотрено %d | кандидатов %d | проставлено %d | пропущено %s | ошибок %d%s",
        stats["scanned"],
        stats["candidates"],
        stats["tagged"],
        stats["skipped"] or "0",
        stats["errors"],
        "" if args.apply else " [dry-run]",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
