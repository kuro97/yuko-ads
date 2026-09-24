#!/usr/bin/env python3
"""Разовая уборка зависших предложений владельца: dry-run по умолчанию.

Зачем: режим approval-first выкатили раньше, чем появился переход
PENDING_OWNER → EXPIRED, поэтому в БД накопились предложения, чей TTL давно
вышел, а lifecycle всё ещё ждёт владельца. Их карточки висят в чате с живыми
кнопками, а нажатие падает на PROPOSAL_EXPIRED. То же самое с DELIVERY_PENDING,
который так и не доехал до Telegram.

Что делает: переводит такие предложения в EXPIRED легальным переходом
lifecycle (services.owner_action_executor.sweep_expired_proposals) и отзывает
неиспользованные callback-токены. Печатает сводку.

Чего НЕ делает: не ходит в Facebook и не отправляет ничего в Telegram — только
локальная БД. Идемпотентен: повторный запуск не найдёт уже погашенные строки.

Примеры:
    python3 scripts/cleanup_stale_proposals.py
    python3 scripts/cleanup_stale_proposals.py --db-path data/decisions.db
    python3 scripts/cleanup_stale_proposals.py --apply \\
        --confirm-production EXPIRE-STALE-OWNER-PROPOSALS
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.owner_action_executor import (  # noqa: E402
    list_stale_proposals,
    sweep_expired_proposals,
)


CONFIRM_PRODUCTION = "EXPIRE-STALE-OWNER-PROPOSALS"
# Те же недорешённые состояния, что и у регулярного крон-свипера: POSTPONED
# раньше не гасил никто, и отложенный висяк вечно блокировал новые карточки
# по тому же объекту.
SWEEP_STATES = ("DELIVERY_PENDING", "PENDING_OWNER", "POSTPONED")


def resolve_db_path(explicit: str) -> Path:
    """Тот же порядок, что у config.load_owner_approval_config, но без секретов."""

    if explicit:
        return Path(explicit).expanduser()
    for name in ("OWNER_ACTION_DB_PATH", "CREATIVE_KB_PATH"):
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value).expanduser()
    return ROOT / "data" / "decisions.db"


def _utcnow() -> datetime:
    """Отдельная точка времени — чтобы тесты не зависели от часов машины."""

    return datetime.now(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Гасит зависшие DELIVERY_PENDING/PENDING_OWNER/POSTPONED старше TTL"
        ),
    )
    parser.add_argument("--db-path", default="")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-production", default="")
    return parser


def _print_summary(rows: Sequence, *, title: str) -> None:
    print(title)
    if not rows:
        print("  — нечего гасить")
        return
    for row in rows:
        print(
            f"  {row['proposal_id']}  {row['state']:<16} "
            f"TTL={row['valid_until']}  {row['summary']}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = resolve_db_path(args.db_path)
    if not db_path.is_file():
        print(f"БД не найдена: {db_path}")
        return 2
    now = _utcnow()

    stale = list_stale_proposals(
        now=now,
        limit=args.limit,
        db_path=db_path,
        states=SWEEP_STATES,
    )
    _print_summary(
        stale,
        title=f"Протухшие предложения в {db_path} ({len(stale)} шт.):",
    )

    if not args.apply:
        print("\nDry-run: ничего не изменено. Для уборки — --apply c подтверждением.")
        return 0
    if args.confirm_production != CONFIRM_PRODUCTION:
        print(f"\nОтказ: для --apply нужен --confirm-production {CONFIRM_PRODUCTION}")
        return 2
    if not stale:
        return 0

    result = sweep_expired_proposals(
        now=now,
        limit=args.limit,
        db_path=db_path,
        states=SWEEP_STATES,
    )
    print(
        f"\nПогашено: {result.expired}, токенов отозвано: {result.tokens_revoked}, "
        f"пропущено (гонка решения): {result.skipped}, ошибки: {result.errors}"
    )
    return 0 if not result.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
