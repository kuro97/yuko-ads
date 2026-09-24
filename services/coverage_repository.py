"""Durable SQLite persistence для read-only мониторинга покрытия."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Mapping


CoverageStatus = Literal["ZERO", "THIN", "OK", "UNKNOWN"]
IncidentKind = Literal["ZERO", "THIN", "UNKNOWN"]

ZERO_REMINDER_DELAY = timedelta(minutes=15)
THIN_REMINDER_DELAY = timedelta(days=1)
DELIVERY_LEASE = timedelta(seconds=60)
DELIVERY_RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=2),
    timedelta(minutes=5),
    timedelta(minutes=15),
)


class CoverageRepositoryError(RuntimeError):
    """Базовая ошибка durable coverage persistence."""


class CoverageDeliveryConflict(CoverageRepositoryError):
    """Delivery lease потерян или уже завершён."""


@dataclass(frozen=True, slots=True)
class CoverageAdset:
    """Один адсет группы: живой ID из кабинета плюс его собственный статус."""

    adset_id: str
    status: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.adset_id, "adset_id"),
            (self.status, "status"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} должен быть непустой строкой")


@dataclass(frozen=True, slots=True)
class CoverageScope:
    """Группа «город × язык» целиком: ВСЕ её адсеты в кабинете.

    Раньше scope нёс ровно один adset_id из статичной карты config.ADSETS —
    владелец пересоздавал адсеты, ID в карте протухал, и страж рапортовал
    ложный ноль по живой группе. Теперь состав приходит из живого каталога
    кабинета, а покрытие считается суммарно по всей группе.

    Пустой ``adsets`` — легальное состояние «состав группы неизвестен»
    (каталог не прочитан или в кабинете не нашлось ни одного адсета группы).
    Такая группа уходит в UNKNOWN, но никогда в ZERO: молчащий каталог не
    доказывает отсутствие рекламы.
    """

    account_id: str
    city: str
    # PRODB — группа PRODB-адсета города.
    language: Literal["L2", "L1", "PRODB"]
    adsets: tuple[CoverageAdset, ...]
    min_active: int = 2

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.account_id, "account_id"),
            (self.city, "city"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} должен быть непустой строкой")
        if self.language not in {"L2", "L1", "PRODB"}:
            raise ValueError("language должен быть L2, L1 или PRODB")
        if not isinstance(self.adsets, tuple) or any(
            not isinstance(adset, CoverageAdset) for adset in self.adsets
        ):
            raise ValueError("adsets должен быть tuple[CoverageAdset, ...]")
        ids = [adset.adset_id for adset in self.adsets]
        if len(set(ids)) != len(ids):
            raise ValueError("adset_id повторяется внутри группы")
        if (
            not isinstance(self.min_active, int)
            or isinstance(self.min_active, bool)
            or self.min_active <= 0
        ):
            raise ValueError("min_active должен быть положительным integer")

    @property
    def group_key(self) -> str:
        """Ключ инцидента — групповой: ротация адсетов не плодит дубли."""
        return "|".join((self.account_id, self.city, self.language))

    @property
    def adset_ids(self) -> frozenset[str]:
        return frozenset(adset.adset_id for adset in self.adsets)


@dataclass(frozen=True, slots=True)
class InventoryPage:
    rows: tuple[Mapping[str, object], ...]
    has_next: bool
    next_cursor: str | None
    scope_observed: bool = True
    complete: bool = True


@dataclass(frozen=True, slots=True)
class CoverageAdsetSnapshot:
    """Как выглядел один адсет группы в момент снимка.

    ``ads_total``/``active_count`` = None означает «объявления не прочитаны»
    (группа ушла в UNKNOWN) — это не то же самое, что прочитанный ноль.
    """

    adset_id: str
    status: str
    ads_total: int | None
    active_count: int | None


@dataclass(frozen=True, slots=True)
class CoverageGroupSnapshot:
    group_key: str
    account_id: str
    city: str
    language: Literal["L2", "L1", "PRODB"]
    adsets: tuple[CoverageAdsetSnapshot, ...]
    min_active: int
    effective_active_count: int | None
    configured_active_count: int | None
    status: CoverageStatus
    inventory_sha256: str

    @property
    def adsets_column(self) -> str:
        """Значение колонки ``coverage_snapshot_groups.adset_id``.

        В группе теперь все адсеты города × языка, а колонка одна — пишем весь
        состав через запятую, чтобы аудит снимка оставался полным. Пустой
        состав пишем маркером группы: иначе UNIQUE(snapshot_id, account_id,
        adset_id) схлопнул бы все такие строки одного снимка в одну.
        """
        if not self.adsets:
            return f"none:{self.city}/{self.language}"
        return ",".join(adset.adset_id for adset in self.adsets)


@dataclass(frozen=True, slots=True)
class CoverageSnapshot:
    snapshot_id: str
    started_at: datetime
    completed_at: datetime
    fetch_complete: bool
    configured_group_count: int
    observed_group_count: int
    page_count: int
    inventory_sha256: str
    error_code: str | None
    groups: tuple[CoverageGroupSnapshot, ...]


@dataclass(frozen=True, slots=True)
class IncidentRun:
    snapshot_id: str
    deduplicated: bool
    opened_count: int
    reminder_count: int
    resolved_count: int
    queued_delivery_count: int


@dataclass(frozen=True, slots=True)
class CoverageDelivery:
    delivery_id: str
    incident_id: str
    lease_token: str
    rendered_text: str
    attempt_no: int


@dataclass(frozen=True, slots=True)
class CoverageDeliveryRun:
    claimed_count: int
    sent_count: int
    retry_count: int
    failed_visible_count: int


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _require_aware(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} должен содержать timezone")


def _iso(value: datetime) -> str:
    _require_aware(value, "datetime")
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str):
        raise CoverageRepositoryError("В БД сохранён невалидный datetime")
    parsed = datetime.fromisoformat(value)
    _require_aware(parsed, "datetime")
    return parsed


def _adsets_payload(group: CoverageGroupSnapshot) -> list[dict[str, object]]:
    return [
        {
            "adset_id": adset.adset_id,
            "status": adset.status,
            "ads_total": adset.ads_total,
            "active_count": adset.active_count,
        }
        for adset in group.adsets
    ]


def _incident_payload(
    *,
    group: CoverageGroupSnapshot,
    incident_kind: str,
    event_type: str,
    reminder_seq: int,
) -> dict[str, object]:
    return {
        "event_type": event_type,
        "incident_kind": incident_kind,
        "group_key": group.group_key,
        "account_id": group.account_id,
        "city": group.city,
        "language": group.language,
        "adsets": _adsets_payload(group),
        "effective_active_count": group.effective_active_count,
        "min_active": group.min_active,
        "reminder_seq": reminder_seq,
    }


def _adset_lines(group: CoverageGroupSnapshot) -> str:
    """Раскладка группы по адсетам — включая выключенные.

    Выключенные адсеты в счёт ACTIVE не идут (их объявления приходят с
    effective_status ADSET_PAUSED/CAMPAIGN_PAUSED), но владельцу нужно видеть
    всю картину: где реклама лежит и в каком состоянии сам адсет.
    """
    if not group.adsets:
        return "  • адсеты группы не обнаружены"
    lines = []
    for adset in group.adsets:
        if adset.active_count is None or adset.ads_total is None:
            lines.append(f"  • {adset.adset_id} [{adset.status}] — не прочитан")
            continue
        lines.append(
            f"  • {adset.adset_id} [{adset.status}] — "
            f"{adset.active_count} ACTIVE из {adset.ads_total}"
        )
    return "\n".join(lines)


def _alert_text(group: CoverageGroupSnapshot, incident_kind: str) -> str:
    if incident_kind == "ZERO":
        return (
            "❗ Критическое покрытие\n"
            f"{group.city}/{group.language}: 0 effective ACTIVE во всей группе\n"
            f"Адсеты группы ({len(group.adsets)}):\n"
            f"{_adset_lines(group)}\n"
            "Монитор только наблюдает и не запускает рекламу автоматически."
        )
    return (
        "⚠️ Тонкое покрытие\n"
        f"{group.city}/{group.language}: "
        f"{group.effective_active_count} из минимум {group.min_active} по группе\n"
        f"Адсеты группы ({len(group.adsets)}):\n"
        f"{_adset_lines(group)}\n"
        "Монитор только наблюдает и не изменяет Facebook."
    )


def _resolution_text(group: CoverageGroupSnapshot) -> str:
    """Закрытие критического инцидента: покрытие есть, тревога снята."""
    return (
        "✅ Покрытие восстановлено\n"
        f"{group.city}/{group.language}: "
        f"{group.effective_active_count} effective ACTIVE в группе\n"
        f"Адсеты группы ({len(group.adsets)}):\n"
        f"{_adset_lines(group)}"
    )


class CoverageRepository:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        if not self._db_path:
            raise ValueError("db_path обязателен")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _insert_incident_event(
        connection: sqlite3.Connection,
        *,
        incident_id: str,
        snapshot_id: str,
        event_type: Literal["OPENED", "REMINDER", "DELIVERED", "RESOLVED"],
        payload: Mapping[str, object],
        now: datetime,
    ) -> None:
        encoded = canonical_json(payload)
        connection.execute(
            """
            INSERT INTO coverage_incident_events (
                event_id, incident_id, snapshot_id, event_type,
                payload_json, payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                incident_id,
                snapshot_id,
                event_type,
                encoded.decode("utf-8"),
                hashlib.sha256(encoded).hexdigest(),
                _iso(now),
            ),
        )

    @staticmethod
    def _insert_outbox(
        connection: sqlite3.Connection,
        *,
        incident_id: str,
        dedupe_key: str,
        rendered_text: str,
        now: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO telegram_delivery_outbox (
                delivery_id, purpose, incident_id, generation, dedupe_key,
                rendered_text, rendered_text_sha256, button_spec_json,
                state, attempts, next_attempt_at, created_at
            ) VALUES (
                ?, 'COVERAGE_ALERT', ?, 0, ?, ?, ?, '[]',
                'PENDING', 0, ?, ?
            )
            """,
            (
                str(uuid.uuid4()),
                incident_id,
                dedupe_key,
                rendered_text,
                hashlib.sha256(rendered_text.encode("utf-8")).hexdigest(),
                _iso(now),
                _iso(now),
            ),
        )

    @staticmethod
    def _queue_alert(
        connection: sqlite3.Connection,
        *,
        incident_id: str,
        snapshot_id: str,
        group: CoverageGroupSnapshot,
        incident_kind: Literal["ZERO", "THIN"],
        reminder_seq: int,
        event_type: Literal["OPENED", "REMINDER"],
        now: datetime,
    ) -> None:
        CoverageRepository._insert_outbox(
            connection,
            incident_id=incident_id,
            dedupe_key=f"coverage:{incident_id}:{reminder_seq}",
            rendered_text=_alert_text(group, incident_kind),
            now=now,
        )
        payload = _incident_payload(
            group=group,
            incident_kind=incident_kind,
            event_type=event_type,
            reminder_seq=reminder_seq,
        )
        CoverageRepository._insert_incident_event(
            connection,
            incident_id=incident_id,
            snapshot_id=snapshot_id,
            event_type=event_type,
            payload=payload,
            now=now,
        )

    @staticmethod
    def _insert_snapshot(
        connection: sqlite3.Connection,
        snapshot: CoverageSnapshot,
    ) -> bool:
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO coverage_snapshots (
                snapshot_id, started_at, completed_at, fetch_complete,
                configured_group_count, observed_group_count, page_count,
                inventory_sha256, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                _iso(snapshot.started_at),
                _iso(snapshot.completed_at),
                int(snapshot.fetch_complete),
                snapshot.configured_group_count,
                snapshot.observed_group_count,
                snapshot.page_count,
                snapshot.inventory_sha256,
                snapshot.error_code,
            ),
        ).rowcount
        if inserted != 1:
            existing = connection.execute(
                """
                SELECT started_at, completed_at, fetch_complete,
                       configured_group_count, observed_group_count, page_count,
                       inventory_sha256, error_code
                FROM coverage_snapshots
                WHERE snapshot_id = ?
                """,
                (snapshot.snapshot_id,),
            ).fetchone()
            expected = (
                _iso(snapshot.started_at),
                _iso(snapshot.completed_at),
                int(snapshot.fetch_complete),
                snapshot.configured_group_count,
                snapshot.observed_group_count,
                snapshot.page_count,
                snapshot.inventory_sha256,
                snapshot.error_code,
            )
            if existing is None or tuple(existing) != expected:
                raise CoverageRepositoryError(
                    "snapshot_id уже занят другим immutable snapshot"
                )
            existing_groups = connection.execute(
                """
                SELECT group_key, account_id, city, language, adset_id,
                       min_active, effective_active_count,
                       configured_active_count, status, inventory_sha256
                FROM coverage_snapshot_groups
                WHERE snapshot_id = ?
                ORDER BY group_key
                """,
                (snapshot.snapshot_id,),
            ).fetchall()
            expected_groups = sorted(
                (
                    group.group_key,
                    group.account_id,
                    group.city,
                    group.language,
                    group.adsets_column,
                    group.min_active,
                    group.effective_active_count,
                    group.configured_active_count,
                    group.status,
                    group.inventory_sha256,
                )
                for group in snapshot.groups
            )
            if [tuple(row) for row in existing_groups] != expected_groups:
                raise CoverageRepositoryError(
                    "snapshot_id уже занят другим immutable group set"
                )
            return False
        for group in snapshot.groups:
            connection.execute(
                """
                INSERT INTO coverage_snapshot_groups (
                    snapshot_id, group_key, account_id, city, language,
                    adset_id, min_active, effective_active_count,
                    configured_active_count, status, inventory_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    group.group_key,
                    group.account_id,
                    group.city,
                    group.language,
                    group.adsets_column,
                    group.min_active,
                    group.effective_active_count,
                    group.configured_active_count,
                    group.status,
                    group.inventory_sha256,
                ),
            )
        return True

    @staticmethod
    def _open_group_incidents(
        connection: sqlite3.Connection,
        group_key: str,
        *,
        city: str,
        language: str,
    ) -> list[sqlite3.Row]:
        """Открытые инциденты группы, включая ключи старого формата и кабинета.

        Ключ стал групповым (``account|city|language``), а до этого нёс в хвосте
        ещё и adset_id (``account|city|language|adset``). Старый ключ — ровно
        новый плюс суффикс, поэтому ловим его префиксом: иначе инцидент,
        открытый по протухшему adset_id, никогда бы не закрылся, а по той же
        группе завёлся бы дубликат.

        Тем же дубликатом оборачивается СМЕНА КАБИНЕТА группы: когда L2
        расщеплённых городов переехал в «ACME cabinet_b», его group_key сменил
        голову. Инцидент, открытый по старому кабинету, точным матчем уже не
        находится — он висел бы OPEN вечно (снимок его группы больше не
        приходит, значит и RESOLVED не отправится), а рядом завёлся бы второй
        по новому ключу. Поэтому группу ищем ещё и по хвосту ``|city|language``
        с любым кабинетом в голове: пара (город, тип) живёт ровно в одном
        кабинете, так что схлопнуть чужие группы этот матч не может.
        """
        def _escape(value: str) -> str:
            return (
                value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )

        same_group_prefix = _escape(group_key + "|") + "%"
        tail = "|" + _escape(city) + "|" + _escape(language)
        return connection.execute(
            """
            SELECT *
            FROM coverage_incidents
            WHERE state = 'OPEN'
              AND (
                    group_key = ?
                 OR group_key LIKE ? ESCAPE '\\'
                 OR group_key LIKE ? ESCAPE '\\'
                 OR group_key LIKE ? ESCAPE '\\'
              )
            ORDER BY opened_at
            """,
            (group_key, same_group_prefix, "%" + tail, "%" + tail + "|%"),
        ).fetchall()

    def record_snapshot_and_process_incidents(
        self,
        snapshot: CoverageSnapshot,
        *,
        now: datetime | None = None,
    ) -> IncidentRun:
        processed_at = now or snapshot.completed_at
        _require_aware(processed_at, "now")
        if snapshot.configured_group_count != len(snapshot.groups):
            raise ValueError("Snapshot должен содержать каждую configured group")
        connection = self._connect()
        opened_count = 0
        reminder_count = 0
        resolved_count = 0
        queued_delivery_count = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not self._insert_snapshot(connection, snapshot):
                connection.commit()
                return IncidentRun(
                    snapshot_id=snapshot.snapshot_id,
                    deduplicated=True,
                    opened_count=0,
                    reminder_count=0,
                    resolved_count=0,
                    queued_delivery_count=0,
                )

            for group in snapshot.groups:
                open_incidents = self._open_group_incidents(
                    connection,
                    group.group_key,
                    city=group.city,
                    language=group.language,
                )
                if group.status == "OK":
                    # Одно «✅ восстановлено» на группу за прогон: старых ключей
                    # по одной группе может быть несколько (ротация адсетов).
                    resolution_sent = False
                    for incident in open_incidents:
                        consecutive_ok = (
                            int(incident["consecutive_complete_ok"]) + 1
                            if snapshot.fetch_complete
                            else 0
                        )
                        if consecutive_ok < 2:
                            connection.execute(
                                """
                                UPDATE coverage_incidents
                                SET latest_snapshot_id = ?,
                                    consecutive_complete_ok = ?,
                                    updated_at = ?
                                WHERE incident_id = ? AND state = 'OPEN'
                                """,
                                (
                                    snapshot.snapshot_id,
                                    consecutive_ok,
                                    _iso(processed_at),
                                    incident["incident_id"],
                                ),
                            )
                            continue
                        updated = connection.execute(
                            """
                            UPDATE coverage_incidents
                            SET state = 'RESOLVED', latest_snapshot_id = ?,
                                consecutive_complete_ok = ?,
                                next_reminder_at = NULL, updated_at = ?,
                                resolved_at = ?
                            WHERE incident_id = ? AND state = 'OPEN'
                            """,
                            (
                                snapshot.snapshot_id,
                                consecutive_ok,
                                _iso(processed_at),
                                _iso(processed_at),
                                incident["incident_id"],
                            ),
                        ).rowcount
                        if updated != 1:
                            raise CoverageRepositoryError(
                                "Incident resolve CAS не выполнен"
                            )
                        incident_id = str(incident["incident_id"])
                        # Критический инцидент закрываем вслух: владелец видел
                        # «0 ACTIVE» и должен увидеть, что тревога снята.
                        # Дедуп — уникальный dedupe_key на инцидент.
                        queue_resolution = (
                            str(incident["incident_kind"]) == "ZERO"
                            and not resolution_sent
                        )
                        if queue_resolution:
                            self._insert_outbox(
                                connection,
                                incident_id=incident_id,
                                dedupe_key=f"coverage:{incident_id}:resolved",
                                rendered_text=_resolution_text(group),
                                now=processed_at,
                            )
                            resolution_sent = True
                            queued_delivery_count += 1
                        self._insert_incident_event(
                            connection,
                            incident_id=incident_id,
                            snapshot_id=snapshot.snapshot_id,
                            event_type="RESOLVED",
                            payload={
                                "group_key": group.group_key,
                                "consecutive_complete_ok": consecutive_ok,
                                "resolution_delivery_queued": queue_resolution,
                            },
                            now=processed_at,
                        )
                        resolved_count += 1
                    continue

                incident_kind: IncidentKind = group.status
                for incident in open_incidents:
                    if str(incident["incident_kind"]) == incident_kind:
                        continue
                    connection.execute(
                        """
                        UPDATE coverage_incidents
                        SET latest_snapshot_id = ?, consecutive_complete_ok = 0,
                            updated_at = ?
                        WHERE incident_id = ? AND state = 'OPEN'
                        """,
                        (
                            snapshot.snapshot_id,
                            _iso(processed_at),
                            incident["incident_id"],
                        ),
                    )

                current = next(
                    (
                        incident
                        for incident in open_incidents
                        if str(incident["incident_kind"]) == incident_kind
                    ),
                    None,
                )
                if current is None:
                    incident_id = str(uuid.uuid4())
                    reminder_seq = 0
                    if incident_kind == "ZERO":
                        next_reminder_at = processed_at + ZERO_REMINDER_DELAY
                    elif incident_kind == "THIN":
                        next_reminder_at = processed_at + THIN_REMINDER_DELAY
                    else:
                        next_reminder_at = None
                    connection.execute(
                        """
                        INSERT INTO coverage_incidents (
                            incident_id, group_key, incident_kind, state,
                            opened_snapshot_id, latest_snapshot_id,
                            consecutive_complete_ok, reminder_seq,
                            next_reminder_at, opened_at, updated_at
                        ) VALUES (?, ?, ?, 'OPEN', ?, ?, 0, ?, ?, ?, ?)
                        """,
                        (
                            incident_id,
                            group.group_key,
                            incident_kind,
                            snapshot.snapshot_id,
                            snapshot.snapshot_id,
                            reminder_seq,
                            _iso(next_reminder_at) if next_reminder_at else None,
                            _iso(processed_at),
                            _iso(processed_at),
                        ),
                    )
                    opened_count += 1
                    if incident_kind in {"ZERO", "THIN"}:
                        self._queue_alert(
                            connection,
                            incident_id=incident_id,
                            snapshot_id=snapshot.snapshot_id,
                            group=group,
                            incident_kind=incident_kind,
                            reminder_seq=reminder_seq,
                            event_type="OPENED",
                            now=processed_at,
                        )
                        queued_delivery_count += 1
                    else:
                        self._insert_incident_event(
                            connection,
                            incident_id=incident_id,
                            snapshot_id=snapshot.snapshot_id,
                            event_type="OPENED",
                            payload=_incident_payload(
                                group=group,
                                incident_kind=incident_kind,
                                event_type="OPENED",
                                reminder_seq=0,
                            ),
                            now=processed_at,
                        )
                    continue

                incident_id = str(current["incident_id"])
                reminder_seq = int(current["reminder_seq"])
                next_reminder_at = (
                    _parse_iso(current["next_reminder_at"])
                    if current["next_reminder_at"] is not None
                    else None
                )
                queued = False
                if (
                    incident_kind in {"ZERO", "THIN"}
                    and next_reminder_at is not None
                    and next_reminder_at <= processed_at
                ):
                    pending = connection.execute(
                        """
                        SELECT 1
                        FROM telegram_delivery_outbox
                        WHERE incident_id = ?
                          AND state IN ('PENDING','LEASED')
                        """,
                        (incident_id,),
                    ).fetchone()
                    if pending is None:
                        reminder_seq += 1
                        self._queue_alert(
                            connection,
                            incident_id=incident_id,
                            snapshot_id=snapshot.snapshot_id,
                            group=group,
                            incident_kind=incident_kind,
                            reminder_seq=reminder_seq,
                            event_type="REMINDER",
                            now=processed_at,
                        )
                        queued = True
                        reminder_count += 1
                        queued_delivery_count += 1
                        next_reminder_at = (
                            processed_at + ZERO_REMINDER_DELAY
                            if incident_kind == "ZERO"
                            else processed_at + THIN_REMINDER_DELAY
                        )
                connection.execute(
                    """
                    UPDATE coverage_incidents
                    SET latest_snapshot_id = ?, consecutive_complete_ok = 0,
                        reminder_seq = ?, next_reminder_at = ?, updated_at = ?
                    WHERE incident_id = ? AND state = 'OPEN'
                    """,
                    (
                        snapshot.snapshot_id,
                        reminder_seq,
                        _iso(next_reminder_at) if next_reminder_at else None,
                        _iso(processed_at),
                        incident_id,
                    ),
                )
                if queued:
                    continue
            connection.commit()
            return IncidentRun(
                snapshot_id=snapshot.snapshot_id,
                deduplicated=False,
                opened_count=opened_count,
                reminder_count=reminder_count,
                resolved_count=resolved_count,
                queued_delivery_count=queued_delivery_count,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_due_deliveries(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int = 20,
    ) -> tuple[CoverageDelivery, ...]:
        _require_aware(now, "now")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit должен быть 1..100")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id обязателен")
        connection = self._connect()
        claims: list[CoverageDelivery] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT delivery_id, incident_id, rendered_text, attempts
                FROM telegram_delivery_outbox
                WHERE purpose = 'COVERAGE_ALERT'
                  AND (
                      (state = 'PENDING' AND next_attempt_at <= ?)
                      OR (state = 'LEASED' AND lease_until <= ?)
                  )
                ORDER BY created_at, delivery_id
                LIMIT ?
                """,
                (_iso(now), _iso(now), limit),
            ).fetchall()
            for row in rows:
                lease_token = f"{worker_id}:{uuid.uuid4()}"
                updated = connection.execute(
                    """
                    UPDATE telegram_delivery_outbox
                    SET state = 'LEASED', attempts = attempts + 1,
                        lease_token = ?, lease_until = ?, last_error_code = NULL
                    WHERE delivery_id = ?
                      AND (
                          (state = 'PENDING' AND next_attempt_at <= ?)
                          OR (state = 'LEASED' AND lease_until <= ?)
                      )
                    """,
                    (
                        lease_token,
                        _iso(now + DELIVERY_LEASE),
                        row["delivery_id"],
                        _iso(now),
                        _iso(now),
                    ),
                ).rowcount
                if updated != 1:
                    continue
                claims.append(
                    CoverageDelivery(
                        delivery_id=str(row["delivery_id"]),
                        incident_id=str(row["incident_id"]),
                        lease_token=lease_token,
                        rendered_text=str(row["rendered_text"]),
                        attempt_no=int(row["attempts"]) + 1,
                    )
                )
            connection.commit()
            return tuple(claims)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_delivery_success(
        self,
        *,
        delivery: CoverageDelivery,
        telegram_chat_id: int,
        telegram_message_id: int,
        now: datetime,
    ) -> None:
        _require_aware(now, "now")
        if (
            not isinstance(telegram_message_id, int)
            or isinstance(telegram_message_id, bool)
            or telegram_message_id <= 0
        ):
            raise ValueError("telegram_message_id должен быть положительным integer")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT d.incident_id, i.latest_snapshot_id, i.incident_kind,
                       i.reminder_seq
                FROM telegram_delivery_outbox d
                JOIN coverage_incidents i ON i.incident_id = d.incident_id
                WHERE d.delivery_id = ? AND d.state = 'LEASED'
                  AND d.lease_token = ?
                """,
                (delivery.delivery_id, delivery.lease_token),
            ).fetchone()
            if row is None:
                raise CoverageDeliveryConflict("DELIVERY_LEASE_LOST")
            updated = connection.execute(
                """
                UPDATE telegram_delivery_outbox
                SET state = 'SENT', lease_token = NULL, lease_until = NULL,
                    telegram_chat_id = ?, telegram_message_id = ?,
                    sent_at = ?, next_attempt_at = NULL
                WHERE delivery_id = ? AND state = 'LEASED'
                  AND lease_token = ?
                """,
                (
                    telegram_chat_id,
                    telegram_message_id,
                    _iso(now),
                    delivery.delivery_id,
                    delivery.lease_token,
                ),
            ).rowcount
            if updated != 1:
                raise CoverageDeliveryConflict("DELIVERY_SUCCESS_CAS_CONFLICT")
            if str(row["incident_kind"]) == "ZERO":
                connection.execute(
                    """
                    UPDATE coverage_incidents
                    SET next_reminder_at = NULL, updated_at = ?
                    WHERE incident_id = ? AND state = 'OPEN'
                    """,
                    (_iso(now), row["incident_id"]),
                )
            self._insert_incident_event(
                connection,
                incident_id=str(row["incident_id"]),
                snapshot_id=str(row["latest_snapshot_id"]),
                event_type="DELIVERED",
                payload={
                    "delivery_id": delivery.delivery_id,
                    "attempt_no": delivery.attempt_no,
                    "telegram_message_id": telegram_message_id,
                    "reminder_seq": int(row["reminder_seq"]),
                },
                now=now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_delivery_failure(
        self,
        *,
        delivery: CoverageDelivery,
        error_code: str,
        now: datetime,
    ) -> Literal["RETRY", "FAILED_VISIBLE"]:
        _require_aware(now, "now")
        if not isinstance(error_code, str) or not error_code.strip():
            raise ValueError("error_code обязателен")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if delivery.attempt_no <= len(DELIVERY_RETRY_DELAYS):
                state = "PENDING"
                next_attempt_at = now + DELIVERY_RETRY_DELAYS[delivery.attempt_no - 1]
                outcome: Literal["RETRY", "FAILED_VISIBLE"] = "RETRY"
            else:
                state = "FAILED_VISIBLE"
                next_attempt_at = None
                outcome = "FAILED_VISIBLE"
            updated = connection.execute(
                """
                UPDATE telegram_delivery_outbox
                SET state = ?, lease_token = NULL, lease_until = NULL,
                    next_attempt_at = ?, last_error_code = ?
                WHERE delivery_id = ? AND state = 'LEASED'
                  AND lease_token = ? AND attempts = ?
                """,
                (
                    state,
                    _iso(next_attempt_at) if next_attempt_at else None,
                    error_code,
                    delivery.delivery_id,
                    delivery.lease_token,
                    delivery.attempt_no,
                ),
            ).rowcount
            if updated != 1:
                raise CoverageDeliveryConflict("DELIVERY_FAILURE_CAS_CONFLICT")
            connection.commit()
            return outcome
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
