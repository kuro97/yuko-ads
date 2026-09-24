"""Сверка галочек Trello с живой рекламой — страховка от запусков мимо контура.

Зачем. Зелёный чек на карточке (``dueComplete``) означает для команды «реклама
живёт». Штатно его ставят два пути: сторож запуска по VERIFIED
(``services/trello_completion.py``) и ``scripts/manual_launch.py``. Но заметная
часть запусков идёт мимо них — разовыми скриптами долива и клонирования
(PRODB-адсеты вне карты роутинга, международка). Такие запуски Trello не трогают,
и карточка остаётся «незапущенной»: сверка находила карточки с живыми
объявлениями, но без галочки.

Что делает. Раз в тик берёт открытые карточки доски и живые объявления всех
оффлайн-кабинетов карты роутинга, сопоставляет их по штатному имени объявления
и ставит галочку карточкам, у которых есть хотя бы одно живое объявление.

Правило сопоставления — только точное, по конвенции ``_expected_city_ad_names``
(``integrations/facebook.py``): ``<Город> | <Карточка>[ / <метка ролика>] [ТЕГ]``.
С имени снимается продуктовый тег и городской префикс до первого ``" | "``;
кандидатов ровно два — тело целиком и тело без последнего сегмента (метка
ролика: штатный запуск добавляет к имени карточки не больше одного сегмента,
а метка — это имя файла, слэша в ней нет). Глубже не режем: усечение до
произвольного префикса отмечало бы чужую короткую карточку («Блогер / Тема»
рядом с «Блогер / Тема / Вариант»). По той же причине в индекс имён входят и
АРХИВНЫЕ карточки: объявление архивной «Блогер / Тема / Вариант» находит её
по полному имени и не проваливается к открытой короткой; отмечаются только
открытые. Нечёткого поиска нет намеренно: ложная галочка хуже пропущенной — сервис её
не снимает, а отмеченную карточку очередь запуска больше не видит. Свободные
имена разовых скриптов («intl_line | Продукт Б без цены | В1») остаются
несопоставленными и видны в логе.

Усечённый кандидат — доказательство слабее точного: если точной карточки нет
нигде (переименована), одиночное объявление «Карточка / метка» упало бы к
открытой карточке-префиксу. Поэтому совпадение через усечение засчитывается
автоматически только когда к карточке привязались объявления с ≥2 разными
метками (мультиассетный запуск — так штатно и именуется); одна метка на
карточку — «слабое» совпадение, оно уходит в ``needs_review`` (WARNING),
галочку ставит человек.

Вторая защита — время: объявление старше карточки (дата в ObjectId Trello) не
может быть её запуском и к ней не привязывается.

Чего не делает. Не снимает галочки (пауза рекламы — не повод «разжаловать»
карточку), не трогает карточки вне целевых колонок и в архивных колонках,
не трогает карточки с неоднозначным именем (две карточки с одним именем,
открытые или архивная рядом с открытой — например L2- и L1-версии
одной карточки — решает человек).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from config import TRELLO_BOARD_ID, TRELLO_DONE_LIST_NAME
from integrations.trello import BASE, redact_trello_secrets, safe_request
from services.product_tags import strip_product_tag


logger = logging.getLogger(__name__)

# Колонки, где галочка означает «запущено». «Готово» — штатная очередь запуска,
# «Вторая линейка» — карточки intl_line, которые живут отдельно от карты городов.
DEFAULT_LIST_NAMES: tuple[str, ...] = (TRELLO_DONE_LIST_NAME, "Вторая линейка")

# Живое объявление — оба статуса ACTIVE. Фильтр FB по effective_status
# пропускает ARCHIVED-строки (объявление «Город | Креатив А» приходило с
# status=ARCHIVED), поэтому статус сверяется локально по обоим полям.
_LIVE_STATUS = "ACTIVE"

# Trello режет выдачу на 1000 строк молча — карточки читаем страницами по
# курсору ``before=<минимальный id страницы>`` до пустой страницы (страницы
# по 100 проходят все карточки без дублей).
_CARDS_PAGE_SIZE = 1000
_MAX_CARD_PAGES = 100

_CITY_SEPARATOR = " | "
_SEGMENT_SEPARATOR = " / "
_WHITESPACE_RE = re.compile(r"\s+")
# id карточки Trello — Mongo ObjectId: первые 4 байта = unix-секунды создания.
_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$")


class ReconcileError(RuntimeError):
    """Источник отдал данные, по которым сверять нельзя (fail-closed)."""


@dataclass(frozen=True, slots=True)
class LiveAd:
    """Живое объявление одного кабинета."""

    ad_id: str
    name: str
    account_id: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BoardCard:
    """Карточка доски — единица сверки. Архивные тоже нужны: они держат имена."""

    card_id: str
    name: str
    list_name: str
    due_complete: bool
    closed: bool = False
    list_closed: bool = False
    created_at: datetime | None = None

    @property
    def open(self) -> bool:
        return not self.closed


@dataclass(frozen=True, slots=True)
class CardMatch:
    """Карточка и живые объявления, которые к ней привязались."""

    card: BoardCard
    ad_ids: tuple[str, ...]
    ad_names: tuple[str, ...]
    # Все объявления пришли через усечённый кандидат с одной меткой — галочку
    # автоматически не ставим (см. докстринг модуля).
    weak: bool = False


@dataclass(frozen=True, slots=True)
class ReconcileRun:
    """Итог одного прохода — что отмечено и что осталось на глаза человеку."""

    to_mark: tuple[CardMatch, ...]
    marked: tuple[str, ...]
    failed: tuple[str, ...]
    ambiguous: tuple[CardMatch, ...]
    needs_review: tuple[CardMatch, ...]
    outside_lists: tuple[CardMatch, ...]
    unmatched_ads: tuple[LiveAd, ...]
    unchecked_without_ads: int
    accounts_scanned: tuple[str, ...]
    accounts_failed: tuple[str, ...]

    @property
    def marked_count(self) -> int:
        return len(self.marked)


# --- Имена -----------------------------------------------------------------


def normalize_name(value: str) -> str:
    """Каноническая форма имени для сравнения: пробелы, регистр, ё."""

    return _WHITESPACE_RE.sub(" ", str(value or "")).strip().casefold().replace("ё", "е")


def card_name_candidates(ad_name: str) -> tuple[str, ...]:
    """Кандидаты имени карточки из имени объявления: тело и тело без метки.

    ``CityA | Креатор Б / PRODB / Тема А 2 [PRODB]`` →
    ``креатор б / prodb / тема а 2``, ``креатор б / prodb``.
    Ровно два кандидата: штатное имя = имя карточки плюс не более одного
    сегмента-метки. Префикс города снимается только до ПЕРВОГО ``" | "``:
    свободные имена с несколькими разделителями остаются как есть и заведомо
    не совпадают ни с одной карточкой.
    """

    base = strip_product_tag(str(ad_name or "")).strip()
    if not base:
        return ()
    _head, separator, tail = base.partition(_CITY_SEPARATOR)
    body = tail if separator else base
    segments = [segment.strip() for segment in body.split(_SEGMENT_SEPARATOR)]
    candidates: list[str] = []
    for length in (len(segments), len(segments) - 1):
        if length <= 0:
            continue
        candidate = normalize_name(_SEGMENT_SEPARATOR.join(segments[:length]))
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def card_created_at(card_id: str) -> datetime | None:
    """Момент создания карточки из её ObjectId; None, если id не той формы."""

    if not _OBJECT_ID_RE.match(str(card_id or "")):
        return None
    return datetime.fromtimestamp(int(card_id[:8], 16), tz=timezone.utc)


def _parse_created_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# --- Источники --------------------------------------------------------------


def _require_str(payload: Mapping[str, object], field: str, context: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ReconcileError(f"{context}_{field}_invalid")
    return value


def _cards_page(board_id: str, before: str | None) -> list[Mapping[str, object]]:
    params: dict[str, object] = {
        "fields": "id,name,dueComplete,closed,idList",
        "limit": _CARDS_PAGE_SIZE,
    }
    if before is not None:
        params["before"] = before
    response = safe_request("GET", f"{BASE}/boards/{board_id}/cards/all", params=params)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ReconcileError("cards_json_invalid") from exc
    if not isinstance(payload, list):
        raise ReconcileError("cards_payload_not_list")
    for item in payload:
        if not isinstance(item, Mapping):
            raise ReconcileError("card_not_object")
    return payload


def _fetch_all_card_rows(board_id: str) -> list[Mapping[str, object]]:
    """Все карточки доски, доказанно до конца: страницы по курсору до пустой."""

    rows: list[Mapping[str, object]] = []
    seen_ids: set[str] = set()
    before: str | None = None
    for _page in range(_MAX_CARD_PAGES):
        page = _cards_page(board_id, before)
        if not page:
            return rows
        page_ids = [_require_str(item, "id", "card") for item in page]
        if seen_ids.intersection(page_ids) or len(set(page_ids)) != len(page_ids):
            raise ReconcileError("cards_paging_duplicate")
        cursor = min(page_ids)
        if before is not None and cursor >= before:
            raise ReconcileError("cards_paging_cursor_invalid")
        seen_ids.update(page_ids)
        rows.extend(page)
        if len(page) < _CARDS_PAGE_SIZE:
            return rows
        before = cursor
    raise ReconcileError("cards_page_limit_exceeded")


def fetch_board_cards(
    board_id: str = TRELLO_BOARD_ID,
) -> tuple[BoardCard, ...]:
    """Все карточки доски (открытые и архивные) с именем колонки.

    Архивные карточки нужны индексу имён (см. докстринг модуля), отмечаются
    только открытые. Сначала карточки, потом колонки (с ``filter=all``): так
    колонка, появившаяся между двумя запросами, уже есть в справочнике, а
    карточки архивных колонок не роняют проход. Снимок валидируется целиком до
    сверки: битая карточка — отказ прохода, а не тихий пропуск (иначе именно
    она и осталась бы без галочки).
    """

    card_rows = _fetch_all_card_rows(board_id)

    lists_response = safe_request(
        "GET",
        f"{BASE}/boards/{board_id}/lists",
        params={"fields": "id,name,closed", "filter": "all"},
    )
    try:
        lists_payload = lists_response.json()
    except ValueError as exc:
        raise ReconcileError("lists_json_invalid") from exc
    if not isinstance(lists_payload, list):
        raise ReconcileError("lists_payload_not_list")
    lists: dict[str, tuple[str, bool]] = {}
    for item in lists_payload:
        if not isinstance(item, Mapping):
            raise ReconcileError("list_not_object")
        closed = item.get("closed")
        if type(closed) is not bool:
            raise ReconcileError("list_closed_invalid")
        lists[_require_str(item, "id", "list")] = (_require_str(item, "name", "list"), closed)

    cards: list[BoardCard] = []
    for item in card_rows:
        card_id = _require_str(item, "id", "card")
        name = _require_str(item, "name", "card")
        list_id = _require_str(item, "idList", "card")
        due_complete = item.get("dueComplete")
        closed = item.get("closed")
        if type(due_complete) is not bool or type(closed) is not bool:
            raise ReconcileError("card_flags_invalid")
        if list_id not in lists:
            raise ReconcileError("card_list_unknown")
        list_name, list_closed = lists[list_id]
        cards.append(
            BoardCard(
                card_id=card_id,
                name=name,
                list_name=list_name,
                due_complete=due_complete,
                closed=closed,
                list_closed=list_closed,
                created_at=card_created_at(card_id),
            )
        )
    return tuple(cards)


def _default_accounts() -> tuple[str, ...]:
    from services.launch_routing import accounts_to_scan

    return tuple(accounts_to_scan())


def _default_inventory(account_id: str) -> list[Mapping[str, object]]:
    from integrations.facebook import fetch_complete_account_ad_inventory

    return fetch_complete_account_ad_inventory("offline", account_id)


def fetch_live_ads(
    *,
    accounts: Callable[[], Sequence[str]] = _default_accounts,
    inventory: Callable[[str], Sequence[Mapping[str, object]]] = _default_inventory,
) -> tuple[tuple[LiveAd, ...], tuple[str, ...], tuple[str, ...]]:
    """Живые объявления всех оффлайн-кабинетов карты роутинга.

    Возвращает (объявления, кабинеты прочитаны, кабинеты не прочитаны).
    Отказ одного кабинета не роняет проход: галочка ставится только по
    ПОЛОЖИТЕЛЬНОМУ факту живого объявления, поэтому неполный инвентарь не
    может дать ложную отметку — только отложить верную до следующего тика.
    """

    account_ids = tuple(
        dict.fromkeys(str(account or "").removeprefix("act_").strip() for account in accounts())
    )
    if not account_ids or any(not account.isdigit() for account in account_ids):
        raise ReconcileError("accounts_invalid")

    ads: list[LiveAd] = []
    scanned: list[str] = []
    failed: list[str] = []
    for account_id in account_ids:
        try:
            account_ads = _live_ads_of_account(account_id, inventory(account_id))
        except Exception as exc:  # noqa: BLE001 — кабинет откладываем, не роняем проход
            logger.warning(
                "trello_check_reconciler: кабинет act_%s не прочитан — %s: %s",
                account_id,
                type(exc).__name__,
                redact_trello_secrets(exc),
            )
            failed.append(account_id)
            continue
        ads.extend(account_ads)
        scanned.append(account_id)
    return tuple(ads), tuple(scanned), tuple(failed)


def _live_ads_of_account(
    account_id: str, rows: Sequence[Mapping[str, object]]
) -> list[LiveAd]:
    """Живые объявления одного кабинета; битая строка — отказ этого кабинета.

    Объявление с пустым именем сопоставиться не может — пропускаем, а не
    роняем проход (апстрим пустое имя пропускает).
    """

    ads: list[LiveAd] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReconcileError("inventory_row_not_object")
        if (
            row.get("status") != _LIVE_STATUS
            or row.get("effective_status") != _LIVE_STATUS
        ):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        ads.append(
            LiveAd(
                ad_id=_require_str(row, "id", "ad"),
                name=name,
                account_id=account_id,
                created_at=_parse_created_time(row.get("created_time")),
            )
        )
    return ads


# --- Сверка -------------------------------------------------------------------


def _ad_can_belong(ad: LiveAd, card: BoardCard) -> bool:
    """Объявление, созданное раньше карточки, не может быть её запуском."""

    if ad.created_at is None or card.created_at is None:
        return True
    return ad.created_at >= card.created_at


def match_ads_to_cards(
    cards: Iterable[BoardCard],
    ads: Iterable[LiveAd],
) -> tuple[dict[str, CardMatch], tuple[LiveAd, ...], frozenset[str]]:
    """Привязывает живые объявления к карточкам доски по точному имени.

    В индексе и открытые, и архивные карточки: полное имя объявления должно
    находить СВОЮ карточку, даже архивную, а не проваливаться к открытой
    карточке-префиксу. Возвращает (совпадения по card_id, объявления без
    карточки, имена, под которыми на доске больше одной карточки).
    """

    cards = tuple(cards)
    by_name: dict[str, list[BoardCard]] = {}
    for card in cards:
        by_name.setdefault(normalize_name(card.name), []).append(card)
    ambiguous_names = frozenset(name for name, group in by_name.items() if len(group) > 1)

    hits: dict[str, list[LiveAd]] = {}
    # Метки объявлений, привязанных через усечённый кандидат; None — точное
    # совпадение (оно само по себе достаточное доказательство).
    labels: dict[str, set[str | None]] = {}
    unmatched: list[LiveAd] = []
    for ad in ads:
        target: list[BoardCard] = []
        label: str | None = None
        candidates = card_name_candidates(ad.name)
        for depth, candidate in enumerate(candidates):
            group = by_name.get(candidate)
            if not group:
                continue
            target = [card for card in group if _ad_can_belong(ad, card)]
            if target:
                label = candidates[0][len(candidate):].strip(" /") if depth else None
                break
        if not target:
            unmatched.append(ad)
            continue
        for card in target:
            hits.setdefault(card.card_id, []).append(ad)
            labels.setdefault(card.card_id, set()).add(label)

    cards_by_id = {card.card_id: card for card in cards}
    matches = {
        card_id: CardMatch(
            card=cards_by_id[card_id],
            ad_ids=tuple(ad.ad_id for ad in group),
            ad_names=tuple(dict.fromkeys(ad.name for ad in group)),
            weak=None not in labels[card_id] and len(labels[card_id]) < 2,
        )
        for card_id, group in hits.items()
    }
    return matches, tuple(unmatched), ambiguous_names


def _mark_card_done_default(card_id: str) -> None:
    from integrations.trello import mark_card_done

    mark_card_done(card_id)


def reconcile_trello_checks(
    *,
    cards: Sequence[BoardCard],
    ads: Sequence[LiveAd],
    accounts_scanned: Sequence[str] = (),
    accounts_failed: Sequence[str] = (),
    list_names: Sequence[str] = DEFAULT_LIST_NAMES,
    apply: bool = True,
    mark_card_done: Callable[[str], None] | None = None,
) -> ReconcileRun:
    """Чистая сверка: карточки + объявления → отметки. Источники подаются снаружи."""

    if mark_card_done is None:
        mark_card_done = _mark_card_done_default
    target_lists = frozenset(list_names)
    matches, unmatched, ambiguous_names = match_ads_to_cards(cards, ads)

    def _in_target(card: BoardCard) -> bool:
        return card.list_name in target_lists and not card.list_closed

    to_mark: list[CardMatch] = []
    ambiguous: list[CardMatch] = []
    needs_review: list[CardMatch] = []
    outside: list[CardMatch] = []
    try:
        from services.auto_launch import resumable_card_ids

        waiting_resume = resumable_card_ids()
    except Exception as exc:  # noqa: BLE001 — нет state → прежнее поведение
        logger.warning("trello_check_reconciler: попытки запуска не прочитаны — %s", exc)
        waiting_resume = set()
    for match in sorted(matches.values(), key=lambda item: (item.card.list_name, item.card.name)):
        card = match.card
        if card.due_complete or card.closed:
            continue
        if card.card_id in waiting_resume:
            # Запущены не все города: галочка выбила бы карточку из дозапуска.
            continue
        if normalize_name(card.name) in ambiguous_names:
            ambiguous.append(match)
        elif not _in_target(card):
            outside.append(match)
        elif match.weak:
            needs_review.append(match)
        else:
            to_mark.append(match)

    matched_ids = set(matches)
    unchecked_without_ads = sum(
        1
        for card in cards
        if card.open
        and not card.due_complete
        and _in_target(card)
        and card.card_id not in matched_ids
    )

    marked: list[str] = []
    failed: list[str] = []
    if apply:
        for match in to_mark:
            card = match.card
            try:
                mark_card_done(card.card_id)
            except Exception as exc:  # noqa: BLE001 — одна карточка не блокирует остальные
                logger.warning(
                    "trello_check_reconciler: карточка %s «%s» не отмечена — %s: %s",
                    card.card_id,
                    card.name,
                    type(exc).__name__,
                    redact_trello_secrets(exc),
                )
                failed.append(card.card_id)
                continue
            logger.info(
                "trello_check_reconciler: галочка на «%s» [%s] по объявлениям %s",
                card.name,
                card.list_name,
                ", ".join(match.ad_ids),
            )
            marked.append(card.card_id)

    for match in ambiguous:
        logger.warning(
            "trello_check_reconciler: имя «%s» у нескольких карточек доски — "
            "галочку не ставлю, решает человек (объявления %s)",
            match.card.name,
            ", ".join(match.ad_ids),
        )
    for match in needs_review:
        logger.warning(
            "trello_check_reconciler: «%s» [%s] — только усечённое совпадение с одной "
            "меткой (%s), галочку не ставлю, подтвердите руками",
            match.card.name,
            match.card.list_name,
            "; ".join(match.ad_names),
        )
    for match in outside:
        logger.warning(
            "trello_check_reconciler: живая реклама у карточки «%s» в колонке «%s»%s "
            "вне целевых — галочку не ставлю",
            match.card.name,
            match.card.list_name,
            " (архивная)" if match.card.list_closed else "",
        )
    if unmatched:
        # INFO, не WARNING: свободные имена разовых скриптов и переименованные
        # карточки живут неделями, ежечасный WARNING по ним — шум, а не сигнал.
        names = sorted(dict.fromkeys(ad.name for ad in unmatched))
        logger.info(
            "trello_check_reconciler: %d живых объявлений без карточки (%d имён): %s",
            len(unmatched),
            len(names),
            "; ".join(names[:20]) + (" …" if len(names) > 20 else ""),
        )

    return ReconcileRun(
        to_mark=tuple(to_mark),
        marked=tuple(marked),
        failed=tuple(failed),
        ambiguous=tuple(ambiguous),
        needs_review=tuple(needs_review),
        outside_lists=tuple(outside),
        unmatched_ads=unmatched,
        unchecked_without_ads=unchecked_without_ads,
        accounts_scanned=tuple(accounts_scanned),
        accounts_failed=tuple(accounts_failed),
    )


def reconcile_trello_checks_from_config(
    *,
    apply: bool = True,
    list_names: Sequence[str] = DEFAULT_LIST_NAMES,
) -> ReconcileRun:
    """Боевой проход: доска из config, кабинеты из карты роутинга."""

    cards = fetch_board_cards()
    ads, scanned, failed = fetch_live_ads()
    if not scanned:
        raise ReconcileError("no_account_inventory")
    return reconcile_trello_checks(
        cards=cards,
        ads=ads,
        accounts_scanned=scanned,
        accounts_failed=failed,
        list_names=list_names,
        apply=apply,
    )
