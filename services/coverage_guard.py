"""Боевая обвязка live-стража покрытия: адаптеры FB/Telegram + прогон крона.

Вынесено из services/coverage_monitor.py осознанно: тот модуль обязан остаться
чистым анализом покрытия без импортов боевых интеграций (граница закреплена
тестом tests/test_live_coverage_monitor.py::
test_coverage_modules_have_no_mutation_or_execution_imports). Здесь живут
только read-only адаптеры и склейка уже готовых кубиков монитора.

Facebook — исключительно чтение: fetch_complete_account_ad_inventory (GET с
доказанной пагинацией). Ни одной мутации в этом модуле нет и быть не может.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Mapping

from services.coverage_monitor import (
    CoverageAdsetDirectory,
    CoverageConfigurationError,
    CoverageMonitorError,
    CoverageTelegramClient,
    LiveFacebookInventoryClient,
    MIN_ACTIVE_PER_GROUP,
    collect_live_coverage,
    configured_coverage_scopes,
    deliver_coverage_alerts,
    is_tracked_group,
    process_coverage_incidents,
)
from services.coverage_repository import (
    CoverageAdset,
    CoverageRepository,
    CoverageScope,
    InventoryPage,
)

logger = logging.getLogger(__name__)


def _normalise_account_id(value: object) -> str | None:
    """Приводит act_123 и 123 к единому виду; иное — None."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.removeprefix("act_").strip()


class FacebookAdsetDirectory:
    """Живой состав групп TRACKED_GROUPS (город × L2/L1 + PRODB) из кабинета — только чтение.

    Зачем вообще: статичная карта config.ADSETS хранит по одному adset_id на
    группу и протухает, как только владелец пересоздаёт адсеты: живая группа
    уезжает на новый adset, а страж смотрит в старый (PAUSED) и шлёт ложный
    критический ноль. Источник истины — кабинет.

    Транспорт тот же, что и у инвентаря объявлений: read-only GET через
    approval_source_facebook._paginate (доказанная cursor-пагинация без
    соседства с мутациями). Классификация имени адсета в город/язык берётся из
    agent.adset_discovery — единственного места, где эта логика живёт.

    Сбой чтения не подменяется офлайн-фолбэком: исключение уходит наверх, и
    configured_coverage_scopes честно переводит все группы в UNKNOWN.
    """

    def list_group_adsets(
        self,
        account_id: str,
    ) -> dict[tuple[str, str], tuple[CoverageAdset, ...]]:
        # Приватные хелперы взяты осознанно: публичного read-only читателя
        # каталога нет, а второй GET-транспорт хуже переиспользования уже
        # доказанного (тот же приём с обоснованием — в FacebookAccountCoverageClient).
        from agent.adset_discovery import _classify_adset  # noqa: SLF001
        from services.approval_source_facebook import _paginate  # noqa: SLF001

        rows = _paginate(
            f"act_{account_id}/adsets",
            {
                "fields": "id,name,status,destination_type,optimization_goal",
                "limit": 500,
            },
        )
        grouped: dict[tuple[str, str], list[CoverageAdset]] = {}
        for row in rows:
            adset_id = row.get("id")
            name = row.get("name")
            status = row.get("status")
            if (
                not isinstance(adset_id, str)
                or not adset_id.strip()
                or not isinstance(name, str)
                or not isinstance(status, str)
                or not status.strip()
            ):
                raise CoverageMonitorError("FB_ADSET_ROW_INVALID")
            city, language = _classify_adset(name, dict(row))
            if not is_tracked_group(city, language):
                continue
            grouped.setdefault((city, language), []).append(
                CoverageAdset(adset_id=adset_id, status=status)
            )
        return {
            key: tuple(sorted(adsets, key=lambda adset: adset.adset_id))
            for key, adsets in grouped.items()
        }


class FacebookAccountCoverageClient:
    """Read-only адаптер live-инвентаря FB под контракт LiveFacebookInventoryClient.

    Источник — services.approval_source_facebook: модуль «force-live Facebook
    evidence без mutation API», где весь транспорт это GET через
    fb_common._throttled_get с доказанной cursor-пагинацией (проверка дублей,
    курсоров и потолка страниц). Взят намеренно вместо integrations.facebook:
    тот модуль соседствует с мутациями, а мониторингу покрытия нельзя иметь
    даже теоретической дороги до них (граница закреплена тестом
    tests/test_live_coverage_monitor.py).

    Один полный инвентарь кабинета на прогон (кэш по account_id): 10 скоупов
    город × L2/L1 обслуживаются одним чтением — берегём CPU-бюджет FB.

    Ошибка чтения кэшируется и переподнимается для каждого скоупа: так все
    группы честно уходят в UNKNOWN (fail-closed), а FB не опрашивается заново
    по каждому из десяти скоупов.
    """

    def __init__(self) -> None:
        self._rows_by_account: dict[str, tuple[Mapping[str, object], ...]] = {}
        self._errors: dict[str, Exception] = {}

    def _account_rows(self, account_id: str) -> tuple[Mapping[str, object], ...]:
        cached_error = self._errors.get(account_id)
        if cached_error is not None:
            raise cached_error
        cached_rows = self._rows_by_account.get(account_id)
        if cached_rows is not None:
            return cached_rows

        # Приватный хелпер берём осознанно: публичного read-only читателя
        # инвентаря в модуле нет, а плодить второй GET-транспорт хуже, чем
        # переиспользовать уже доказанный. Тот же приём с обоснованием применён
        # в самом approval_source_facebook (fb_common._throttled_get).
        from services.approval_source_facebook import (
            _load_account_inventory,  # noqa: SLF001
        )

        try:
            rows = tuple(_load_account_inventory(account_id))
        except Exception as exc:
            self._errors[account_id] = exc
            raise
        self._rows_by_account[account_id] = rows
        return rows

    def fetch_page(
        self,
        scope: CoverageScope,
        after: str | None,
    ) -> InventoryPage:
        """Отдаёт строки всех адсетов группы одной завершённой страницей."""
        if after is not None:
            # Полный инвентарь кабинета уже вычитан целиком — курсора не бывает.
            raise CoverageMonitorError("COVERAGE_UNEXPECTED_CURSOR")
        rows = self._account_rows(scope.account_id)
        adset_ids = scope.adset_ids
        scope_rows = tuple(
            {
                "id": row.get("id"),
                # account_id доказан самим путём запроса act_<id>/ads, а в строках
                # Graph его нет — проставляем запрошенный scope явно.
                "account_id": _normalise_account_id(row.get("account_id"))
                or scope.account_id,
                "adset_id": row.get("adset_id"),
                "status": row.get("status"),
                "effective_status": row.get("effective_status"),
            }
            for row in rows
            if row.get("adset_id") in adset_ids
        )
        return InventoryPage(
            rows=scope_rows,
            has_next=False,
            next_cursor=None,
            scope_observed=True,
            complete=True,
        )


class CoverageAlertTelegramSender:
    """Подтверждаемая доставка coverage-алерта: message_id или исключение.

    Своя отправка вместо notifications.send_telegram нужна потому, что durable
    outbox помечает алерт SENT только при подтверждённом message_id, а
    send_telegram возвращает лишь bool. Успешно доставленный алерт дублируется
    в ленту уведомлений тем же уровнем, что и send_critical_alert (critical для
    нулевого покрытия, warning для тонкого), — дедуп при этом остаётся за
    outbox, поэтому лента не засоряется каждые 30 минут.
    """

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        timeout_seconds: int = 10,
    ) -> None:
        if bot_token is None:
            from config import TELEGRAM_BOT_TOKEN

            bot_token = TELEGRAM_BOT_TOKEN
        if not isinstance(bot_token, str) or not bot_token.strip():
            raise CoverageConfigurationError("TELEGRAM_BOT_TOKEN не настроен")
        self._bot_token = bot_token
        self._timeout_seconds = timeout_seconds

    def send_message(self, chat_id: int, text: str) -> int:
        import requests

        response = requests.post(
            f"https://api.telegram.org/bot{self._bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=self._timeout_seconds,
        )
        payload = response.json()
        if payload.get("ok") is not True:
            raise CoverageMonitorError("TELEGRAM_SEND_REJECTED")
        message = payload.get("result")
        message_id = message.get("message_id") if isinstance(message, dict) else None
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            raise CoverageMonitorError("TELEGRAM_MESSAGE_ID_MISSING")
        self._mirror_to_feed(text)
        return message_id

    @staticmethod
    def _mirror_to_feed(text: str) -> None:
        """Кладёт доставленный алерт в ленту уведомлений; сбой не рушит доставку."""
        try:
            from services.notifications import EVENT_ALERT, add_event

            # Маркер ставит coverage_repository: ❗ = ZERO, ⚠️ = THIN,
            # ✅ = закрытие критического инцидента (не тревога, а отбой).
            if text.startswith("❗"):
                level = "critical"
            elif text.startswith("✅"):
                level = "info"
            else:
                level = "warning"
            lines = text.split("\n")
            add_event(
                EVENT_ALERT,
                lines[0],
                "\n".join(lines[1:]),
                level=level,
                meta={"source": "coverage_guard"},
            )
        except Exception as exc:
            logger.warning(
                "Coverage alert не попал в ленту уведомлений: %s",
                type(exc).__name__,
            )


def _resolve_coverage_db_path(db_path: str | None = None) -> str:
    """Возвращает путь к runtime-БД, где живут таблицы покрытия (миграция 022)."""
    if db_path is not None:
        return str(db_path)

    from agent import database

    if database.DB_PATH is None:
        raise CoverageMonitorError("Runtime БД не инициализирована")
    return str(database.DB_PATH)


def _resolve_coverage_chat_id(telegram_chat_id: int | None = None) -> int:
    """Возвращает chat_id для алертов покрытия; без него доставка бессмысленна."""
    if telegram_chat_id is not None:
        return int(telegram_chat_id)

    from config import TELEGRAM_CHAT_ID

    if TELEGRAM_CHAT_ID is None or not str(TELEGRAM_CHAT_ID).strip():
        raise CoverageConfigurationError("TELEGRAM_CHAT_ID не настроен")
    return int(TELEGRAM_CHAT_ID)


def live_coverage_overview(
    *,
    client: LiveFacebookInventoryClient | None = None,
    directory: CoverageAdsetDirectory | None = None,
    now: datetime | None = None,
    min_active: int = MIN_ACTIVE_PER_GROUP,
) -> dict:
    """Живой срез покрытия в форме analyze_coverage — для отчётного блока.

    Тот же путь, которым уже ходит страж (run_coverage_guard): состав группы
    берётся из ЖИВОГО каталога адсетов кабинета, а не из локальной creative_kb
    и не из статичной config.ADSETS. Именно из-за локального источника блок
    показывал «CityC/L2 — 0» при живом ACTIVE в новом ручном адсете: в
    creative_kb этого адсета ещё нет, и колонки adset_id там нет в принципе.

    Инцидентов НЕ ведёт и в репозиторий ничего не пишет (repository=None) —
    это read-only срез для текста отчёта, дедуп алертов остаётся за стражем.

    Returns:
        Ключи analyze_coverage (thin/empty/ok_count/by_group/generated_at)
        плюс:
          unknown — группы, инвентарь которых прочитать не удалось (у live
                    есть четвёртое состояние, которого нет у локального
                    анализа; молча выдавать его за 0 или за «в норме» нельзя);
          source="live", fetch_complete — полнота снимка.
    """
    started_at = now or datetime.now(timezone.utc)
    scopes = configured_coverage_scopes(
        min_active=min_active,
        directory=directory or FacebookAdsetDirectory(),
    )
    snapshot = collect_live_coverage(
        client=client or FacebookAccountCoverageClient(),
        scopes=scopes,
        repository=None,
        now=started_at,
    )

    thin: list[dict] = []
    empty: list[dict] = []
    unknown: list[dict] = []
    by_group: dict[str, int | None] = {}
    ok_count = 0
    for group in snapshot.groups:
        entry = {
            "city": group.city,
            "adset_type": group.language,
            "count": group.effective_active_count,
        }
        by_group[f"{group.city}/{group.language}"] = group.effective_active_count
        if group.status == "ZERO":
            empty.append({**entry, "count": 0})
        elif group.status == "THIN":
            thin.append(entry)
        elif group.status == "OK":
            ok_count += 1
        else:
            unknown.append(entry)

    for bucket in (thin, empty, unknown):
        bucket.sort(key=lambda item: (item["city"], item["adset_type"]))

    return {
        "thin": thin,
        "empty": empty,
        "unknown": unknown,
        "ok_count": ok_count,
        "by_group": by_group,
        "generated_at": snapshot.completed_at.isoformat(),
        "source": "live",
        "fetch_complete": snapshot.fetch_complete,
    }


def run_coverage_guard(
    *,
    client: LiveFacebookInventoryClient | None = None,
    directory: CoverageAdsetDirectory | None = None,
    telegram_client: CoverageTelegramClient | None = None,
    repository: CoverageRepository | None = None,
    db_path: str | None = None,
    telegram_chat_id: int | None = None,
    worker_id: str | None = None,
    now: datetime | None = None,
    min_active: int = MIN_ACTIVE_PER_GROUP,
) -> dict:
    """Live-страж покрытия: собирает инвентарь, ведёт инциденты, шлёт алерты.

    Полный цикл одного прогона крона (только чтение FB):
      1. configured_coverage_scopes — группы TRACKED_GROUPS (расщеплённые города × L2/L1
         плюс PRODB всех городов карты), состав каждой группы берётся из ЖИВОГО
         каталога адсетов кабинета (config.ADSETS в этом пути не участвует:
         статичная карта протухает при ротации адсетов);
      2. collect_live_coverage — live-инвентарь и классификация каждой группы
         суммарно по всем её адсетам (ZERO / THIN / OK / UNKNOWN);
      3. process_coverage_incidents — durable инциденты и постановка алертов в
         outbox; ноль ACTIVE ставится в очередь с next_attempt_at = сейчас,
         поэтому критический алерт уходит в этом же прогоне;
      4. deliver_coverage_alerts — отправка с подтверждением по message_id.

    Дедуп уже обеспечен coverage_repository и НЕ дублируется здесь: повторный
    ZERO-алерт вообще не ставится в очередь (record_delivery_success обнуляет
    next_reminder_at после подтверждённой доставки), THIN напоминает не чаще
    раза в сутки (THIN_REMINDER_DELAY). Своего state-файла страж не держит.

    Fail-closed: ошибка чтения FB (и каталога адсетов, и объявлений) превращает
    группу в UNKNOWN, снимок получает fetch_complete=False, и в ответе ok=False
    — «всё в норме» при недоступном Facebook не рапортуется никогда, ложный
    ноль по нечитаемой группе тоже.

    Returns:
        Сводка прогона: статусы по группам, счётчики инцидентов и доставки.
    """
    guard_started_at = now or datetime.now(timezone.utc)
    resolved_repository = repository or CoverageRepository(
        _resolve_coverage_db_path(db_path)
    )
    resolved_chat_id = _resolve_coverage_chat_id(telegram_chat_id)
    resolved_worker_id = worker_id or f"coverage-guard-{uuid.uuid4().hex[:12]}"

    scopes = configured_coverage_scopes(
        min_active=min_active,
        directory=directory or FacebookAdsetDirectory(),
    )
    snapshot = collect_live_coverage(
        client=client or FacebookAccountCoverageClient(),
        scopes=scopes,
        repository=None,
        now=guard_started_at,
    )
    incidents = process_coverage_incidents(
        snapshot,
        repository=resolved_repository,
        now=guard_started_at,
    )
    delivery = deliver_coverage_alerts(
        repository=resolved_repository,
        client=telegram_client or CoverageAlertTelegramSender(),
        telegram_chat_id=resolved_chat_id,
        worker_id=resolved_worker_id,
        now=guard_started_at,
    )

    by_status: dict[str, list[str]] = {"ZERO": [], "THIN": [], "OK": [], "UNKNOWN": []}
    for group in snapshot.groups:
        by_status[group.status].append(f"{group.city}/{group.language}")

    summary = {
        "snapshot_id": snapshot.snapshot_id,
        "fetch_complete": snapshot.fetch_complete,
        "ok": snapshot.fetch_complete,
        "zero": sorted(by_status["ZERO"]),
        "thin": sorted(by_status["THIN"]),
        "unknown": sorted(by_status["UNKNOWN"]),
        "ok_count": len(by_status["OK"]),
        "opened_count": incidents.opened_count,
        "reminder_count": incidents.reminder_count,
        "resolved_count": incidents.resolved_count,
        "queued_delivery_count": incidents.queued_delivery_count,
        "sent_count": delivery.sent_count,
        "retry_count": delivery.retry_count,
        "failed_visible_count": delivery.failed_visible_count,
    }
    if snapshot.fetch_complete:
        logger.info(
            "Страж покрытия: zero=%s thin=%s ok=%d алертов отправлено=%d",
            summary["zero"],
            summary["thin"],
            summary["ok_count"],
            delivery.sent_count,
        )
    else:
        logger.warning(
            "Страж покрытия: инвентарь неполный (unknown=%s) — «всё ок» не рапортуем",
            summary["unknown"],
        )
    return summary
