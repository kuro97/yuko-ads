"""
Монитор покрытия рекламой — проверяет, хватает ли активных объявлений
по каждому городу и типу адсета (L2/L1).

Логика:
- Читает активные ады из creative_kb (effective_status='ACTIVE') локально.
- Группирует по (city, adset_type).
- Группа с 0 активных → ПУСТО (критично, ❗).
- Группа с < MIN_ACTIVE_PER_GROUP активных → ТОНКО (⚠️).
- Раз в день в 09:xx по локальному времени шлёт сводку в Telegram.

Никаких FB/AMO-вызовов — только чтение локальной БД.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Protocol

from services.coverage_repository import (
    CoverageAdset,
    CoverageAdsetSnapshot,
    CoverageDeliveryRun,
    CoverageGroupSnapshot,
    CoverageRepository,
    CoverageScope,
    CoverageSnapshot,
    IncidentRun,
    InventoryPage,
    canonical_sha256,
)

logger = logging.getLogger(__name__)

# Локальное время (UTC+5 по умолчанию, настраивается)
_TZ_LOCAL = timezone(timedelta(hours=5))

# Минимум активных объявлений на группу (city, adset_type) — ниже = «тонко»
MIN_ACTIVE_PER_GROUP = 2

# Города и типы, за которыми следим
# CityF в TRACKED_CITIES НЕ входит намеренно: PRODA-пары L2+L1 у неё под
# наблюдением нет (страж выдавал бы вечное «CityF/L2: 0 активных»). Под
# наблюдением у CityF только PRODB — отдельной группой в TRACKED_GROUPS.
# TRACKED_CITIES / TRACKED_TYPES оставлены для обратной совместимости импортов;
# состав групп определяет TRACKED_GROUPS.
TRACKED_CITIES = ["CityA", "CityB", "CityC", "CityD", "CityE"]
TRACKED_TYPES = ["L2", "L1"]

# Группы (город, тип) под наблюдением: PRODA-пары L2/L1 расщеплённых городов плюс
# PRODB-адсеты «Owner | PRODB | MQL | SO <Город> | ver1» (тип PRODB, cabinet_b).
# Порядок: сначала PRODA-пары (первая группа — CityA/L2, на неё
# опираются тесты), затем PRODB.
TRACKED_GROUPS: tuple[tuple[str, str], ...] = tuple(
    (city, adset_type) for city in TRACKED_CITIES for adset_type in TRACKED_TYPES
) + (
    ("CityA", "PRODB"),
    ("CityB", "PRODB"),
    ("CityC", "PRODB"),
    ("CityD", "PRODB"),
    ("CityE", "PRODB"),
    ("CityF", "PRODB"),
)
_TRACKED_GROUP_SET = frozenset(TRACKED_GROUPS)


def is_tracked_group(city: str, adset_type: str) -> bool:
    """Пара (город, тип) под наблюдением стража покрытия."""
    return (city, adset_type) in _TRACKED_GROUP_SET

# State-файл для гейта «раз в день»
_COVERAGE_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "coverage_state.json"


# ---------------------------------------------------------------------------
# Утилиты state-файла
# ---------------------------------------------------------------------------

def _load_coverage_state() -> dict:
    """Загружает состояние монитора покрытия из файла."""
    default: dict = {"last_sent_date": None}
    if not _COVERAGE_STATE_FILE.exists():
        return default
    try:
        data = json.loads(_COVERAGE_STATE_FILE.read_text(encoding="utf-8"))
        default.update(data)
        return default
    except Exception as exc:
        logger.warning("Не удалось загрузить coverage_state.json: %s", exc)
        return default


def _save_coverage_state(state: dict) -> None:
    """Сохраняет состояние монитора покрытия (tmp + rename для атомарности)."""
    _COVERAGE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _COVERAGE_STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_COVERAGE_STATE_FILE)
    except Exception as exc:
        logger.error("Не удалось сохранить coverage_state.json: %s", exc)
        raise


def should_send_coverage_report(now: datetime, last_sent_date: str | None) -> bool:
    """Гейт: локальный час == 9 И дата не совпадает с last_sent_date.

    Args:
        now: текущее время (с tzinfo или без — если без, считается что уже локальное время)
        last_sent_date: строка 'YYYY-MM-DD' последней отправки или None
    """
    if now.tzinfo is not None:
        now_local = now.astimezone(_TZ_LOCAL)
    else:
        now_local = now

    if now_local.hour != 9:
        return False

    today_str = now_local.date().isoformat()
    if last_sent_date is not None and last_sent_date == today_str:
        return False

    return True


# ---------------------------------------------------------------------------
# Анализ покрытия
# ---------------------------------------------------------------------------

def analyze_coverage(min_per_group: int = MIN_ACTIVE_PER_GROUP) -> dict:
    """Собирает активные ады из локальной БД и анализирует покрытие.

    Группирует по (city, adset_type) — только группы из TRACKED_GROUPS
    (PRODA-пары L2/L1 расщеплённых городов + PRODB всех городов карты).
    Группа с 0 → empty (критично).
    Группа с count < min_per_group → thin (мало).

    Args:
        min_per_group: порог «мало». По умолчанию MIN_ACTIVE_PER_GROUP=2.

    Returns:
        {
            "thin": [{"city": ..., "adset_type": ..., "count": ...}, ...],
            "empty": [...],  # то же, count всегда 0
            "ok_count": int,  # число групп, которые в норме
            "by_group": {("city", "adset_type"): count, ...},
            "generated_at": "ISO-строка"
        }
    """
    # Импортируем здесь — чтобы модуль грузился без инициализированной БД
    from services.shadow_report import _fetch_ads_from_local_db

    ads = _fetch_ads_from_local_db()

    # Считаем активные ады по группам (city, adset_type)
    counts: dict[tuple, int] = {}
    for ad in ads:
        city = (ad.get("city") or "").strip()
        adset_type = (ad.get("adset_type") or "").strip()
        if not is_tracked_group(city, adset_type):
            continue
        key = (city, adset_type)
        counts[key] = counts.get(key, 0) + 1

    # Проверяем все ожидаемые группы (TRACKED_GROUPS)
    thin = []
    empty = []
    ok_count = 0

    for city, adset_type in TRACKED_GROUPS:
        key = (city, adset_type)
        count = counts.get(key, 0)
        entry = {"city": city, "adset_type": adset_type, "count": count}

        if count == 0:
            empty.append(entry)
        elif count < min_per_group:
            thin.append(entry)
        else:
            ok_count += 1

    # Сортируем: сначала по городу, потом по типу — для стабильного вывода
    thin.sort(key=lambda x: (x["city"], x["adset_type"]))
    empty.sort(key=lambda x: (x["city"], x["adset_type"]))

    # by_group только для отслеживаемых групп (с нулями)
    by_group = {
        f"{city}/{adset_type}": counts.get((city, adset_type), 0)
        for city, adset_type in TRACKED_GROUPS
    }

    return {
        "thin": thin,
        "empty": empty,
        "ok_count": ok_count,
        "by_group": by_group,
        "generated_at": datetime.now(_TZ_LOCAL).isoformat(),
    }


# ---------------------------------------------------------------------------
# Форматирование отчёта
# ---------------------------------------------------------------------------

def format_coverage_report(data: dict) -> str:
    """Форматирует срез покрытия в короткий Telegram-HTML.

    Пример вывода:
        📊 <b>Покрытие рекламой</b>

        ❗ CityD/L2 — 0 активных
        ⚠️ CityA/L1 — 1 актив

        ✅ 8 групп в норме

    Формат сохранён с обоих источников. Дополнительно, когда они есть:
      - «❓ Город/Тип — инвентарь не прочитан» для live-групп в состоянии
        UNKNOWN: у живого среза есть четвёртое состояние, и выдавать его за
        ноль или за «в норме» нельзя;
      - хвостовая пометка источника, если срез пришёл НЕ из живого кабинета
        (тихо подсунуть отстающую локальную базу вместо живых групп нельзя).

    Args:
        data: результат coverage_guard.live_coverage_overview() либо
            analyze_coverage(); ключ ``source`` различает их.

    Returns:
        HTML-строка для parse_mode="HTML" в Telegram (≤ ~20 строк).
    """
    lines = ["📊 <b>Покрытие рекламой</b>"]

    empty = data.get("empty", [])
    thin = data.get("thin", [])
    unknown = data.get("unknown", [])
    ok_count = data.get("ok_count", 0)

    if not empty and not thin and not unknown:
        lines.append("")
        lines.append("✅ Покрытие в норме — везде достаточно активных реклам")
    else:
        lines.append("")
        # Сначала пустые (критичнее)
        for entry in empty:
            lines.append(f"❗ {entry['city']}/{entry['adset_type']} — 0 активных")
        # Потом тонкие
        for entry in thin:
            count = entry["count"]
            word = "актив" if count == 1 else "актива"
            lines.append(f"⚠️ {entry['city']}/{entry['adset_type']} — {count} {word}")
        # Непрочитанные группы — честно, отдельным знаком (это не ноль)
        for entry in unknown:
            lines.append(
                f"❓ {entry['city']}/{entry['adset_type']} — инвентарь не прочитан"
            )

        lines.append("")
        lines.append(f"✅ В норме: {ok_count} групп")

    if data.get("source") != "live":
        lines.append("")
        lines.append("<i>Данные по локальной базе (может отставать)</i>")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Публичная функция отправки
# ---------------------------------------------------------------------------

def collect_coverage_overview() -> dict:
    """Срез покрытия для отчёта: живые группы кабинета, локальная БД — фолбэк.

    Основной путь — coverage_guard.live_coverage_overview: состав групп берётся
    из живого каталога адсетов, поэтому новый ручной адсет владельца виден
    сразу. Локальная creative_kb этого не умеет (в ней нет adset_id и она
    отстаёт на такт синка) — именно из-за неё блок рапортовал «CityC/L2 — 0»
    при живом ACTIVE в новом адсете.

    Фолбэк на локальную базу не молчаливый: срез помечается source="local",
    и format_coverage_report дописывает это в текст.
    """
    try:
        from services.coverage_guard import live_coverage_overview

        return live_coverage_overview()
    except Exception as exc:
        logger.warning(
            "Монитор покрытия: живой срез недоступен (%s) — падаем на локальную базу",
            exc,
        )
        data = analyze_coverage()
        data["source"] = "local"
        data["unknown"] = []
        return data


def send_coverage_report() -> bool:
    """Собирает данные о покрытии и отправляет отчёт в Telegram (канал ads).

    Только чтение — никаких действий с FB/AMO.

    Returns:
        True если отправка прошла успешно, False иначе.
    """
    from services.notifications import send_telegram

    try:
        data = collect_coverage_overview()
        text = format_coverage_report(data)
        ok = send_telegram(text, channel="ads")
        if ok:
            logger.info(
                "Монитор покрытия: отчёт отправлен — empty=%d thin=%d ok=%d",
                len(data["empty"]),
                len(data["thin"]),
                data["ok_count"],
            )
        else:
            logger.warning("Монитор покрытия: send_telegram вернул False")
        return ok
    except Exception as exc:
        logger.error("send_coverage_report: ошибка — %s", exc)
        return False


# ---------------------------------------------------------------------------
# Полный live-инвентарь Facebook (только чтение)
# ---------------------------------------------------------------------------

_KNOWN_FB_STATUSES = frozenset(
    {
        "ACTIVE",
        "ADSET_PAUSED",
        "ARCHIVED",
        "CAMPAIGN_PAUSED",
        "DELETED",
        "DISAPPROVED",
        "IN_PROCESS",
        "PAUSED",
        "PENDING_BILLING_INFO",
        "PENDING_REVIEW",
        "PREAPPROVED",
        "WITH_ISSUES",
    }
)


class LiveFacebookInventoryClient(Protocol):
    """Минимальный read-only контракт одной страницы Facebook inventory."""

    def fetch_page(
        self,
        scope: CoverageScope,
        after: str | None,
    ) -> InventoryPage:
        """Возвращает одну страницу строго по адсетам переданной группы."""


class CoverageAdsetDirectory(Protocol):
    """Живой каталог адсетов кабинета, разложенный по группам город × язык."""

    def list_group_adsets(
        self,
        account_id: str,
    ) -> Mapping[tuple[str, str], tuple[CoverageAdset, ...]]:
        """{(город, язык): все адсеты группы}; при сбое чтения — исключение."""


class CoverageTelegramClient(Protocol):
    """Минимальный контракт подтверждаемой Telegram-доставки."""

    def send_message(self, chat_id: int, text: str) -> int:
        """Возвращает Telegram message_id подтверждённого сообщения."""


class CoverageMonitorError(RuntimeError):
    """Ошибка live coverage monitor."""


class CoverageConfigurationError(CoverageMonitorError):
    """Конфигурация не задаёт однозначный coverage scope."""


def _normalise_account_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.removeprefix("act_").strip()


def _static_group_adsets() -> dict[tuple[str, str], tuple[CoverageAdset, ...]]:
    """Офлайн-фолбэк состава групп из статичной карты config.ADSETS.

    Карта хранит по одному adset_id на группу и протухает при каждой ротации
    адсетов владельцем — истина живёт в кабинете. Используется только там, где
    живой каталог недоступен по определению (офлайн-прогоны, тесты).

    Описывает только PRODA-пары L2/L1 (TRACKED_CITIES × TRACKED_TYPES): PRODB в
    config.ADSETS нет, и группы PRODB в фолбэке остаются без состава → UNKNOWN.
    """
    from config import ADSETS

    grouped: dict[tuple[str, str], tuple[CoverageAdset, ...]] = {}
    seen_adsets: set[str] = set()
    for city in TRACKED_CITIES:
        city_adsets = ADSETS.get(city)
        if not isinstance(city_adsets, Mapping):
            raise CoverageConfigurationError(f"Нет ADSETS scope для города {city}")
        for language in TRACKED_TYPES:
            adset_id = city_adsets.get(language)
            if not isinstance(adset_id, str) or not adset_id.strip():
                raise CoverageConfigurationError(
                    f"Нет ADSETS scope для {city}/{language}"
                )
            if adset_id in seen_adsets:
                raise CoverageConfigurationError(
                    f"adset_id повторяется в coverage scopes: {adset_id}"
                )
            seen_adsets.add(adset_id)
            grouped[(city, language)] = (
                CoverageAdset(adset_id=adset_id, status="UNKNOWN"),
            )
    return grouped


def configured_coverage_scopes(
    *,
    account_id: str | None = None,
    min_active: int = MIN_ACTIVE_PER_GROUP,
    directory: CoverageAdsetDirectory | None = None,
) -> tuple[CoverageScope, ...]:
    """Строит scopes групп TRACKED_GROUPS по ЖИВОМУ составу адсетов КАБИНЕТА ПАРЫ.

    Группы — PRODA-пары «город × L2/L1» расщеплённых городов плюс PRODB-адсеты (тип PRODB)
    всех городов карты (страж обязан видеть PRODB).

    Группа — это все адсеты города и языка сразу, а не один ID из конфига:
    владелец пересоздаёт адсеты, и статичная карта немедленно врёт (живая
    группа уезжает на новый adset, а страж присылает ложный ноль).

    Кабинет группы берётся из карты роутинга (services/launch_routing.py), а не
    из одного config.FB_ACCOUNT_ID: после миграции L2 расщеплённых городов
    живёт в «ACME cabinet_b», а в cabinet_a остались СПЯЩИЕ L2-дубли. Смотреть на
    cabinet_a значило бы каждые полчаса слать критический ноль по живым группам —
    у объявлений спящего адсета effective_status = ADSET_PAUSED, то есть ноль
    эффективно активных.

    ``account_id`` — принудительный кабинет для ВСЕХ групп (ручной вызов и
    тесты); карта в этом случае не спрашивается.

    ``directory=None`` — офлайн-фолбэк на config.ADSETS с предупреждением в
    лог. Боевой страж (services.coverage_guard) всегда передаёт живой каталог.

    Сбой каталога не превращается в ложный ноль: состав группы остаётся пустым,
    и группа уезжает в UNKNOWN (fail-closed) — ровно как при ошибке чтения
    объявлений. Так же ведёт себя пара, кабинет которой не удалось определить.
    """
    from config import FB_ACCOUNT_ID

    default_account_id = _normalise_account_id(account_id or FB_ACCOUNT_ID)
    if default_account_id is None:
        raise CoverageConfigurationError("FB_ACCOUNT_ID не настроен")

    forced_account_id = _normalise_account_id(account_id) if account_id else None
    pair_accounts = _coverage_pair_accounts(forced_account_id)

    catalogs: dict[str, Mapping[tuple[str, str], tuple[CoverageAdset, ...]]] = {}
    if directory is None:
        logger.warning(
            "configured_coverage_scopes: живой каталог адсетов не передан — "
            "офлайн-фолбэк config.ADSETS"
        )
        # config.ADSETS описывает ТОЛЬКО дефолтный кабинет, причём для L2 расщеплённых
        # городов там лежат спящие id после миграции в cabinet_b. Отдать этот
        # каталог другому кабинету — значит искать cabinet_a-адсеты в cabinet_b,
        # не найти их и выдать ложный критический ноль по живой группе. Поэтому
        # чужим кабинетам статический фолбэк не достаётся: их группы UNKNOWN.
        static_catalog = _static_group_adsets()
        catalogs = {default_account_id: static_catalog}
    else:
        for account in sorted({acc for acc in pair_accounts.values() if acc}):
            try:
                catalogs[account] = directory.list_group_adsets(account)
            except Exception as exc:
                logger.warning(
                    "Живой каталог адсетов act_%s не прочитан (%s) — "
                    "его группы UNKNOWN",
                    account,
                    type(exc).__name__,
                )
                catalogs[account] = {}

    scopes: list[CoverageScope] = []
    for city, language in TRACKED_GROUPS:
        pair_account = pair_accounts.get((city, language))
        if not pair_account:
            # Пара без кабинета (исключена из карты) — состав неизвестен,
            # группа уедет в UNKNOWN, а не в ложный ноль покрытия.
            scopes.append(
                CoverageScope(
                    account_id=default_account_id,
                    city=city,
                    language=language,
                    adsets=(),
                    min_active=min_active,
                )
            )
            continue
        grouped = catalogs.get(pair_account) or {}
        adsets = grouped.get((city, language)) or ()
        scopes.append(
            CoverageScope(
                account_id=pair_account,
                city=city,
                language=language,
                adsets=tuple(adsets),
                min_active=min_active,
            )
        )
    return tuple(scopes)


def _coverage_pair_accounts(
    forced_account_id: str | None,
) -> dict[tuple[str, str], str | None]:
    """Кабинет каждой пары (город, тип) для стража покрытия.

    ``forced_account_id`` перекрывает карту целиком (ручной вызов/тесты).
    Недоступная карта не подменяется дефолтным кабинетом: пары остаются без
    кабинета и уезжают в UNKNOWN — «состав неизвестен» честнее, чем ложный ноль
    по живой группе, уехавшей в другой кабинет.
    """
    pairs = list(TRACKED_GROUPS)
    if forced_account_id:
        return {pair: forced_account_id for pair in pairs}
    try:
        from services.launch_routing import route_account
    except Exception as exc:  # noqa: BLE001 — карта нечитаема = UNKNOWN
        logger.warning(
            "configured_coverage_scopes: карта роутинга недоступна (%s) — "
            "все группы UNKNOWN",
            type(exc).__name__,
        )
        return {pair: None for pair in pairs}
    accounts: dict[tuple[str, str], str | None] = {}
    for city, language in pairs:
        try:
            accounts[(city, language)] = _normalise_account_id(
                route_account(city, language)
            )
        except Exception as exc:  # noqa: BLE001 — сбой карты = UNKNOWN, не ноль
            logger.warning(
                "configured_coverage_scopes: кабинет %s/%s не определён (%s)",
                city,
                language,
                type(exc).__name__,
            )
            accounts[(city, language)] = None
    return accounts


def _unknown_group(
    scope: CoverageScope,
    *,
    error_code: str,
) -> CoverageGroupSnapshot:
    return CoverageGroupSnapshot(
        group_key=scope.group_key,
        account_id=scope.account_id,
        city=scope.city,
        language=scope.language,
        adsets=tuple(
            CoverageAdsetSnapshot(
                adset_id=adset.adset_id,
                status=adset.status,
                ads_total=None,
                active_count=None,
            )
            for adset in scope.adsets
        ),
        min_active=scope.min_active,
        effective_active_count=None,
        configured_active_count=None,
        status="UNKNOWN",
        inventory_sha256=canonical_sha256(
            {"group_key": scope.group_key, "error_code": error_code}
        ),
    )


def _classify_scope(
    *,
    client: LiveFacebookInventoryClient,
    scope: CoverageScope,
    max_pages_per_scope: int,
) -> tuple[CoverageGroupSnapshot, int, bool]:
    if not scope.adsets:
        # Состав группы неизвестен — это UNKNOWN, а не ноль покрытия.
        return _unknown_group(scope, error_code="NO_ADSETS_DISCOVERED"), 0, False

    rows: list[dict[str, object]] = []
    seen_ad_ids: set[str] = set()
    seen_cursors: set[str] = set()
    after: str | None = None
    page_count = 0
    error_code: str | None = None
    scope_adset_ids = scope.adset_ids

    while page_count < max_pages_per_scope:
        try:
            page = client.fetch_page(scope, after)
        except Exception as exc:
            logger.warning(
                "Live coverage fetch не завершён для %s: %s",
                scope.group_key,
                type(exc).__name__,
            )
            error_code = "FETCH_ERROR"
            break
        page_count += 1
        if not isinstance(page, InventoryPage):
            error_code = "INVALID_PAGE"
            break
        if not page.complete or not page.scope_observed:
            error_code = "INCOMPLETE_SCOPE"
            break

        for raw_row in page.rows:
            if not isinstance(raw_row, Mapping):
                error_code = "INVALID_ROW"
                break
            ad_id = raw_row.get("id")
            adset_id = raw_row.get("adset_id")
            account_id = _normalise_account_id(raw_row.get("account_id"))
            status = raw_row.get("status")
            effective_status = raw_row.get("effective_status")
            if (
                not isinstance(ad_id, str)
                or not ad_id.strip()
                or ad_id in seen_ad_ids
                or account_id != scope.account_id
                or not isinstance(adset_id, str)
                or adset_id not in scope_adset_ids
            ):
                error_code = "SCOPE_MISMATCH"
                break
            if status not in _KNOWN_FB_STATUSES or effective_status not in _KNOWN_FB_STATUSES:
                error_code = "UNKNOWN_STATUS"
                break
            seen_ad_ids.add(ad_id)
            rows.append(
                {
                    "id": ad_id,
                    "account_id": account_id,
                    "adset_id": adset_id,
                    "status": status,
                    "effective_status": effective_status,
                }
            )
        if error_code is not None:
            break
        if not page.has_next:
            break
        if (
            not isinstance(page.next_cursor, str)
            or not page.next_cursor
            or page.next_cursor in seen_cursors
        ):
            error_code = "INCOMPLETE_PAGINATION"
            break
        seen_cursors.add(page.next_cursor)
        after = page.next_cursor
    else:
        error_code = "PAGE_LIMIT_EXCEEDED"

    if error_code is not None:
        return (
            _unknown_group(scope, error_code=error_code),
            page_count,
            False,
        )

    # Покрытие считается суммарно по группе: живой адсет тянет за всю группу,
    # выключенные сами по себе ничего не дают (их объявления приходят с
    # effective_status ADSET_PAUSED/CAMPAIGN_PAUSED), но остаются в деталях.
    ads_total_by_adset: dict[str, int] = {}
    active_by_adset: dict[str, int] = {}
    for row in rows:
        row_adset_id = str(row["adset_id"])
        ads_total_by_adset[row_adset_id] = ads_total_by_adset.get(row_adset_id, 0) + 1
        if row["effective_status"] == "ACTIVE":
            active_by_adset[row_adset_id] = active_by_adset.get(row_adset_id, 0) + 1

    effective_active_count = sum(
        row["effective_status"] == "ACTIVE" for row in rows
    )
    configured_active_count = sum(row["status"] == "ACTIVE" for row in rows)
    if effective_active_count == 0:
        status = "ZERO"
    elif effective_active_count < scope.min_active:
        status = "THIN"
    else:
        status = "OK"
    return (
        CoverageGroupSnapshot(
            group_key=scope.group_key,
            account_id=scope.account_id,
            city=scope.city,
            language=scope.language,
            adsets=tuple(
                CoverageAdsetSnapshot(
                    adset_id=adset.adset_id,
                    status=adset.status,
                    ads_total=ads_total_by_adset.get(adset.adset_id, 0),
                    active_count=active_by_adset.get(adset.adset_id, 0),
                )
                for adset in scope.adsets
            ),
            min_active=scope.min_active,
            effective_active_count=effective_active_count,
            configured_active_count=configured_active_count,
            status=status,
            inventory_sha256=canonical_sha256(
                sorted(rows, key=lambda row: str(row["id"]))
            ),
        ),
        page_count,
        True,
    )


def collect_live_coverage(
    *,
    client: LiveFacebookInventoryClient,
    scopes: tuple[CoverageScope, ...] | None = None,
    repository: CoverageRepository | None = None,
    now: datetime | None = None,
    max_pages_per_scope: int = 100,
) -> CoverageSnapshot:
    """Собирает complete paginated inventory и опционально фиксирует snapshot."""
    started_at = now or datetime.now(timezone.utc)
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("now должен содержать timezone")
    if (
        not isinstance(max_pages_per_scope, int)
        or isinstance(max_pages_per_scope, bool)
        or max_pages_per_scope <= 0
    ):
        raise ValueError("max_pages_per_scope должен быть положительным integer")
    resolved_scopes = scopes or configured_coverage_scopes()
    if not resolved_scopes:
        raise CoverageConfigurationError("Не настроено ни одной coverage group")
    if len({scope.group_key for scope in resolved_scopes}) != len(resolved_scopes):
        raise CoverageConfigurationError("Coverage scopes должны быть уникальны")

    groups: list[CoverageGroupSnapshot] = []
    page_count = 0
    observed_group_count = 0
    for scope in resolved_scopes:
        group, group_page_count, observed = _classify_scope(
            client=client,
            scope=scope,
            max_pages_per_scope=max_pages_per_scope,
        )
        groups.append(group)
        page_count += group_page_count
        observed_group_count += int(observed)

    completed_at = now or datetime.now(timezone.utc)
    fetch_complete = observed_group_count == len(resolved_scopes)
    inventory_document = [
        {
            "group_key": group.group_key,
            "status": group.status,
            "inventory_sha256": group.inventory_sha256,
        }
        for group in sorted(groups, key=lambda item: item.group_key)
    ]
    snapshot = CoverageSnapshot(
        snapshot_id=str(uuid.uuid4()),
        started_at=started_at,
        completed_at=completed_at,
        fetch_complete=fetch_complete,
        configured_group_count=len(resolved_scopes),
        observed_group_count=observed_group_count,
        page_count=page_count,
        inventory_sha256=canonical_sha256(inventory_document),
        error_code=None if fetch_complete else "INCOMPLETE_INVENTORY",
        groups=tuple(groups),
    )
    if repository is not None:
        repository.record_snapshot_and_process_incidents(
            snapshot,
            now=completed_at,
        )
    return snapshot


def process_coverage_incidents(
    snapshot: CoverageSnapshot,
    *,
    repository: CoverageRepository,
    now: datetime | None = None,
) -> IncidentRun:
    """Атомарно фиксирует snapshot и обновляет durable incidents."""
    return repository.record_snapshot_and_process_incidents(
        snapshot,
        now=now or snapshot.completed_at,
    )


def deliver_coverage_alerts(
    *,
    repository: CoverageRepository,
    client: CoverageTelegramClient,
    telegram_chat_id: int,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 20,
) -> CoverageDeliveryRun:
    """Отправляет leased alerts; SENT возможен только с Telegram message_id."""
    delivery_time = now or datetime.now(timezone.utc)
    deliveries = repository.claim_due_deliveries(
        worker_id=worker_id,
        now=delivery_time,
        limit=limit,
    )
    sent_count = 0
    retry_count = 0
    failed_visible_count = 0
    for delivery in deliveries:
        try:
            message_id = client.send_message(
                telegram_chat_id,
                delivery.rendered_text,
            )
            if (
                not isinstance(message_id, int)
                or isinstance(message_id, bool)
                or message_id <= 0
            ):
                raise CoverageMonitorError("TELEGRAM_MESSAGE_ID_MISSING")
        except Exception as exc:
            logger.warning(
                "Coverage alert delivery не подтверждён: %s",
                type(exc).__name__,
            )
            outcome = repository.record_delivery_failure(
                delivery=delivery,
                error_code="TELEGRAM_SEND_FAILED",
                now=delivery_time,
            )
            retry_count += int(outcome == "RETRY")
            failed_visible_count += int(outcome == "FAILED_VISIBLE")
            continue
        # Ошибки durable persistence критичны и не маскируются под Telegram retry.
        repository.record_delivery_success(
            delivery=delivery,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=message_id,
            now=delivery_time,
        )
        sent_count += 1
    return CoverageDeliveryRun(
        claimed_count=len(deliveries),
        sent_count=sent_count,
        retry_count=retry_count,
        failed_visible_count=failed_visible_count,
    )
