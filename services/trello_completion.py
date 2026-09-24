"""Отметка «запущено» на карточке Trello — потребитель очереди TRELLO_COMPLETE.

Кто ставит задачу. Сторож запуска: как только созданные объявления реально
подтверждены в Facebook, переход в VERIFIED в той же транзакции кладёт строку
``purpose='TRELLO_COMPLETE'`` в ``telegram_delivery_outbox``
(``services/launch_repository.py``). До этого модуля у той строки не было ни
одного потребителя — очередь копилась, а зелёный чек на карточке не появлялся.

Почему отметка живёт здесь, а не в авто-запуске. Единственный прежний вызов
``mark_card_done`` сидел в reconciliation ``services/auto_launch.py`` под гейтом
``checker_mode=ENFORCE`` (дефолт — ``observe``) и суточным кроном 10:00 CityA.
Боевой путь «предложение → одобрение владельца → gateway CREATE» через тот код
не проходит вовсе, поэтому отметка не ставилась даже при успешном запуске.
Здесь она не зависит ни от режима автопилота, ни от часа: подтвердили
объявления — отметили карточку ближайшим тиком доставки.

Отмечаем по VERIFIED, а не по CREATE: зелёный чек на карточке читается людьми
как «реклама живёт», и ставить его по факту принятого запроса значило бы врать
при откате на стороне Facebook.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from integrations.trello import redact_trello_secrets


logger = logging.getLogger(__name__)


def _card_is_gone(exc: Exception) -> bool:
    """404 от Trello: карточки больше нет, повторять запрос бессмысленно."""

    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404

# В owner_action_proposal_targets запуска subject_id — это голый card_id
# (agent/launcher.py). Префикс "card:" живёт только внутри action manifest
# (services/action_manifests._subject_id), поэтому снимаем его защитно, а не
# требуем: иначе достаточно одного расхождения между двумя носителями, чтобы
# карточка молча осталась без отметки.
_CARD_SUBJECT_PREFIX = "card:"


@dataclass(frozen=True, slots=True)
class TrelloCompletionRun:
    """Итог одного прохода: сколько карточек отмечено, сколько не поддалось."""

    completed: tuple[str, ...]
    skipped: tuple[str, ...]
    failed: tuple[str, ...]

    @property
    def completed_count(self) -> int:
        return len(self.completed)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _pending_rows(
    connection: sqlite3.Connection,
    *,
    limit: int,
) -> list[sqlite3.Row]:
    """Подтверждённые запуски, чью карточку ещё не отмечали.

    Отбор идёт по журналу ``trello_completion_log``, а не по состоянию строки
    outbox: состояние 'SENT' там означает отправленное сообщение Telegram и
    требует message_id, которого у отметки в Trello быть не может.
    """

    return connection.execute(
        """
        SELECT d.delivery_id, d.proposal_id, t.subject_id
        FROM telegram_delivery_outbox d
        JOIN owner_action_proposal_targets t
          ON t.proposal_id = d.proposal_id AND t.ordinal = 0
        WHERE d.purpose = 'TRELLO_COMPLETE'
          AND t.action_kind = 'CREATE_AD'
          AND NOT EXISTS (
              SELECT 1 FROM trello_completion_log l
              WHERE l.delivery_id = d.delivery_id
          )
        ORDER BY d.created_at, d.delivery_id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def _record(
    connection: sqlite3.Connection,
    *,
    delivery_id: str,
    proposal_id: str,
    card_id: str,
    outcome: str,
    reason_code: str | None,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO trello_completion_log (
            delivery_id, proposal_id, card_id, outcome, reason_code,
            attempts, created_at
        ) VALUES (?, ?, ?, ?, ?, 1, ?)
        """,
        (delivery_id, proposal_id, card_id, outcome, reason_code, _iso(now)),
    )


def complete_trello_cards(
    db_path: str | Path,
    *,
    mark_card_done: Callable[[str], None] | None = None,
    now: datetime | None = None,
    limit: int = 50,
) -> TrelloCompletionRun:
    """Отмечает карточки подтверждённых запусков. Повторный вызов безвреден.

    Идемпотентность двойная: ``PUT dueComplete=true`` сам по себе повторяем, а
    журнал не даёт сделать даже лишний сетевой вызов. Сбой одной карточки не
    трогает остальные и не пишет журнал — строка останется в выборке и уйдёт
    следующим тиком.
    """

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("now должен быть timezone-aware")
    if mark_card_done is None:
        from integrations.trello import mark_card_done as _mark

        mark_card_done = _mark

    connection = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    completed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    try:
        for row in _pending_rows(connection, limit=limit):
            delivery_id = str(row["delivery_id"])
            proposal_id = str(row["proposal_id"])
            subject_id = str(row["subject_id"] or "").strip()
            card_id = (
                subject_id[len(_CARD_SUBJECT_PREFIX):]
                if subject_id.startswith(_CARD_SUBJECT_PREFIX)
                else subject_id
            )
            if not card_id:
                # Запуск без карточки Trello (например, восстановление слота):
                # отмечать нечего, но и возвращаться к строке незачем.
                skipped.append(delivery_id)
                _record(
                    connection,
                    delivery_id=delivery_id,
                    proposal_id=proposal_id,
                    card_id="-",
                    outcome="SKIPPED",
                    reason_code="NO_TRELLO_CARD",
                    now=moment,
                )
                continue
            try:
                mark_card_done(card_id)
            except Exception as exc:
                if _card_is_gone(exc):
                    # Карточку удалили из Trello — отмечать больше нечего и
                    # никогда не будет. Закрываем строку журналом, иначе она
                    # вечно занимает голову выборки и вытесняет живые.
                    skipped.append(delivery_id)
                    _record(
                        connection,
                        delivery_id=delivery_id,
                        proposal_id=proposal_id,
                        card_id=card_id,
                        outcome="SKIPPED",
                        reason_code="CARD_NOT_FOUND",
                        now=moment,
                    )
                    continue
                # Trello лёг или ответил ошибкой — журнал не пишем, чтобы строка
                # вернулась следующим тиком. Раньше это исключение улетало в
                # общий except прогона и гасило всю пачку.
                # Текст requests-исключения содержит URL с key/token, поэтому в
                # лог он идёт только через redact_trello_secrets.
                failed.append(delivery_id)
                logger.warning(
                    "complete_trello_cards: карточка %s не отмечена — %s",
                    card_id,
                    redact_trello_secrets(exc),
                )
                continue
            _record(
                connection,
                delivery_id=delivery_id,
                proposal_id=proposal_id,
                card_id=card_id,
                outcome="COMPLETED",
                reason_code=None,
                now=moment,
            )
            completed.append(delivery_id)
    finally:
        connection.close()
    return TrelloCompletionRun(
        completed=tuple(completed),
        skipped=tuple(skipped),
        failed=tuple(failed),
    )


def complete_trello_cards_from_config(
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> TrelloCompletionRun:
    """Боевая точка входа: путь к БД берётся из конфигурации контура."""

    from config import load_owner_approval_config

    settings = load_owner_approval_config()
    return complete_trello_cards(settings.db_path, now=now, limit=limit)
