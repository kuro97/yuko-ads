"""Сторож-инвариант полной автономии: карточка паузы у владельца = поломка.

История: несколько разных регрессий подряд («включено, но не
исполняется»), и каждую обнаруживал ВЛАДЕЛЕЦ по карточкам в телеге, а не
система. Этот сторож переворачивает контроль: проверяется не «ключ включён»,
а РЕЗУЛЬТАТ — при включённой полной автономии PAUSE-карточек у владельца
существовать не должно. Появилась — немедленный критический алерт с
идентификаторами, чтобы чинить до того, как владелец откроет Telegram.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Окно свежести: смотрим только недавно отправленные карточки, чтобы один и
# тот же застарелый случай не будил владельца каждые 15 минут.
LEAK_WINDOW_MINUTES = 20


def find_pause_card_leaks(
    db_path: str | Path,
    *,
    now: datetime | None = None,
    window_minutes: int = LEAK_WINDOW_MINUTES,
) -> list[dict]:
    """PAUSE-карточки, отправленные владельцу за окно. Пусто = инвариант цел."""

    moment = now or datetime.now(timezone.utc)
    since = (moment - timedelta(minutes=window_minutes)).isoformat()
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT d.proposal_id, d.sent_at, substr(p.summary, 1, 120) AS summary
            FROM telegram_delivery_outbox d
            JOIN owner_action_proposals p USING (proposal_id)
            WHERE d.state = 'SENT'
              AND d.purpose = 'OWNER_PROPOSAL'
              AND d.sent_at >= ?
              AND p.proposal_kind = 'PAUSE'
            ORDER BY d.sent_at
            """,
            (since,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def check_and_alert(db_path: str | Path, *, now: datetime | None = None) -> int:
    """Проверка инварианта + алерт. Возвращает число утёкших карточек."""

    from services.autonomous_pause import is_full_pause_autonomy_enabled

    if not is_full_pause_autonomy_enabled():
        return 0
    leaks = find_pause_card_leaks(db_path, now=now)
    if not leaks:
        return 0
    listing = "\n".join(
        f"• {leak['proposal_id'][:8]} ({leak['sent_at'][:16]}): "
        f"{' · '.join(str(leak['summary']).splitlines())[:80]}"
        for leak in leaks[:8]
    )
    try:
        from services.notifications import send_critical_alert

        send_critical_alert(
            "Полная автономия пауз ТЕЧЁТ: карточки ушли владельцу",
            f"При pause_all_candidates=true владельцу отправлено "
            f"{len(leaks)} PAUSE-карточек за {LEAK_WINDOW_MINUTES} мин — "
            f"самоодобрение не сработало, режим сломан.\n{listing}",
        )
    except Exception as exc:  # noqa: BLE001 — алерт не роняет крон
        logger.error("autonomy_invariant: алерт не отправлен — %s", exc)
    logger.error(
        "autonomy_invariant: инвариант нарушен — %d PAUSE-карточек у владельца: %s",
        len(leaks),
        [leak["proposal_id"][:8] for leak in leaks],
    )
    return len(leaks)
