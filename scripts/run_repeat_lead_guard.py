#!/usr/bin/env python3
"""Запуск стража повторных заявок (логика — services/repeat_lead_guard.py).

fblead.com при повторной FB-заявке сбрасывает ответственного активного лида
на «Биржу лидов» — страж возвращает лид менеджеру и ставит ему задачу.

Без --apply — dry-run: только лог того, что было бы сделано, без записи в AMO
и без сохранения state.

Примеры:
    python scripts/run_repeat_lead_guard.py --minutes 1440   # обкатка за сутки
    python scripts/run_repeat_lead_guard.py --apply          # боевой прогон крона
"""

import argparse
import logging
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from services.repeat_lead_guard import run  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("repeat_lead_guard")


def main() -> int:
    parser = argparse.ArgumentParser(description="Страж повторных заявок AMO")
    parser.add_argument("--minutes", type=int, default=45,
                        help="окно сканирования событий назад (по умолчанию 45)")
    parser.add_argument("--apply", action="store_true",
                        help="реально писать в AMO (иначе dry-run)")
    parser.add_argument("--limit", type=int, default=30,
                        help="максимум возвратов за прогон")
    args = parser.parse_args()

    stats = run(window_minutes=args.minutes, apply=args.apply, limit=args.limit)
    log.info(
        "Итог: сбросов %d | возвращено %d | задач %d | пропущено %s | ошибок %d%s",
        stats["flips"],
        stats["restored"],
        stats["tasks"],
        stats["skipped"] or "0",
        stats["errors"],
        "" if args.apply else " [dry-run]",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
