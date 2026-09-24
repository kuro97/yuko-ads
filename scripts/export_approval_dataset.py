#!/usr/bin/env python3
"""Экспорт датасета обучения по owner-approval решениям в JSONL (только чтение).

По каждому proposal выгружается полная связанная история:
  * сам proposal — kind (LAUNCH/PAUSE/UNPAUSE/SCALE/ASSET_RECOVERY), origin,
    summary, plan_json, хэши, срок годности;
  * claims (owner_action_proposal_targets) — город, язык, adset, account,
    intended_payload;
  * решение владельца (owner_action_decisions) — approve/reject/postpone,
    кто (owner_user_id) и когда (recorded_at);
  * исполнение (owner_execution_jobs + owner_action_attempts) — состояния,
    попытки, provider-результаты;
  * верификация запуска (launch_watchdogs + launch_verification_observations) —
    подтверждён ли живой ACTIVE;
  * события (owner_action_events) и lifecycle;
  * метрики по горизонтам IMMEDIATE/D1/D3/D7/D30 (owner_action_outcomes).

Схема таблиц — migrations/022_owner_approval_redesign.sql.

БД открывается строго в режиме `mode=ro` (+ PRAGMA query_only) внутри
services.owner_training_export — скрипт не делает ни одной записи в БД.
Секреты (callback-токены, permit secrets) не выгружаются.

Запуск:
    python scripts/export_approval_dataset.py --since 2026-07-01 --out dataset.jsonl
    python scripts/export_approval_dataset.py --db data/decisions.db --since 2026-07-01 \
        --until 2026-07-27 --out /tmp/approvals.jsonl
"""

from __future__ import annotations

import argparse
import sys
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from services.owner_training_export import (  # noqa: E402  (после правки sys.path)
    OwnerTrainingExportError,
    export_owner_training_dataset,
)

# Путь к БД по умолчанию — основная runtime-БД проекта
_DEFAULT_DB = "data/decisions.db"

# Сколько дней выгружаем, если --since не задан
_DEFAULT_WINDOW_DAYS = 30


def _parse_moment(value: str) -> datetime:
    """Разбирает YYYY-MM-DD или полный ISO-8601; naive считается UTC.

    Используется как argparse `type=`, поэтому непарсящееся значение argparse
    сам превращает в понятную ошибку и выход с кодом 2 — без трейсбека.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"ожидается YYYY-MM-DD или ISO-8601, получено {value!r}"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export_approval_dataset",
        description="Read-only выгрузка owner-approval датасета в JSONL",
    )
    parser.add_argument(
        "--db",
        default=_DEFAULT_DB,
        help=f"Путь к SQLite БД (по умолчанию {_DEFAULT_DB}); открывается только на чтение",
    )
    parser.add_argument(
        "--since",
        type=_parse_moment,
        help=(
            "Начало периода по created_at proposal, включительно, YYYY-MM-DD "
            f"(по умолчанию {_DEFAULT_WINDOW_DAYS} дней назад)"
        ),
    )
    parser.add_argument(
        "--until",
        type=_parse_moment,
        help="Конец периода, НЕ включительно, YYYY-MM-DD (по умолчанию — сейчас)",
    )
    parser.add_argument(
        "--out",
        help="Файл для JSONL (по умолчанию stdout)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    until = args.until or datetime.now(timezone.utc)
    since = args.since or (until - timedelta(days=_DEFAULT_WINDOW_DAYS))

    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        print(f"БД не найдена: {db_path}", file=sys.stderr)
        return 2

    # Пишем в файл только после успешного открытия БД; stdout — через nullcontext.
    try:
        if args.out:
            out_path = Path(args.out).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            sink_ctx = out_path.open("w", encoding="utf-8")
        else:
            sink_ctx = nullcontext(sys.stdout)
        with sink_ctx as sink:
            summary = export_owner_training_dataset(
                date_from=since,
                date_to=until,
                sink=sink,
                db_path=db_path,
            )
    except OwnerTrainingExportError as exc:
        print(f"Экспорт не выполнен: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Не удалось записать выгрузку: {exc}", file=sys.stderr)
        return 1

    print(
        "Экспортировано: proposals={} decisions={} claims={} attempts={} "
        "outcomes={} observations={} bytes={} sha256={}".format(
            summary.proposal_count,
            summary.decision_count,
            summary.claim_count,
            summary.attempt_count,
            summary.outcome_count,
            summary.verification_observation_count,
            summary.byte_count,
            summary.content_sha256,
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
