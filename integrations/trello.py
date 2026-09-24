import logging
import hashlib
import json
import math
import re
import uuid
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any, Iterable
from config import TRELLO_API_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID, TRELLO_DONE_LIST_NAME

logger = logging.getLogger(__name__)


BASE = "https://api.trello.com/1"
AUTH = {"key": TRELLO_API_KEY, "token": TRELLO_TOKEN}
_MAX_ACTION_PAGES = 10_000
_TRELLO_OBJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Connection pooling — переиспользуем TCP соединения
session = requests.Session()
session.params = AUTH
session.timeout = 30  # 30 сек таймаут на все запросы

# Патчим request чтобы таймаут применялся всегда
_orig_request = session.request
def _request_with_timeout(*args, **kwargs):
    kwargs.setdefault("timeout", 30)
    return _orig_request(*args, **kwargs)
session.request = _request_with_timeout


# --- Гигиена ошибок Trello (не утекать key/token) ---
#
# session.params = AUTH → key и token уходят в query-строку КАЖДОГО запроса.
# У requests текст HTTPError содержит полный URL: "... for url: .../cards?key=..&token=..".
# Если такой exception отдать клиенту (detail=str(e)) или залогировать как есть —
# утекают боевые Trello-креды. Ниже: маскирование секретов + единый безопасный
# wrapper, который наружу отдаёт только correlation id, без сырого URL/секретов.

# key/token/access_token в query-строках → key=*** (регистронезависимо).
_TRELLO_SECRET_RE = re.compile(r"(?i)\b(key|token|access_token)=[^&\s\"']+")


def redact_trello_secrets(text: object) -> str:
    """Убирает key/token/access_token из строки (URL/exception) перед логом/ответом."""
    return _TRELLO_SECRET_RE.sub(r"\1=***", str(text))


class TrelloError(Exception):
    """Ошибка обращения к Trello БЕЗ секретов и без сырого URL.

    correlation_id — короткий ref для сопоставления с redacted-записью в логе;
    клиенту отдаём только его, а не текст исходного requests-исключения.
    """

    def __init__(self, correlation_id: str):
        self.correlation_id = correlation_id
        super().__init__(f"Trello request failed (ref: {correlation_id})")


def safe_request(method: str, url: str, **kwargs):
    """Единая обёртка Trello-запросов с безопасной обработкой ошибок.

    Успех → Response (raise_for_status уже пройден). Сетевой сбой или не-2xx →
    в лог пишем redacted-детали + correlation id и поднимаем TrelloError без
    секретов/URL. key/token по-прежнему передаются через session.params, а НЕ
    явным аргументом params — чтобы не плодить их в коде и логах вызова.
    """
    try:
        resp = session.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        cid = uuid.uuid4().hex[:12]
        logger.warning("trello %s failed [ref=%s]: %s", method, cid, redact_trello_secrets(exc))
        raise TrelloError(cid) from None


def _raise_trello_protocol_error(context: str) -> None:
    """Поднимает безопасную ошибку на некорректный ответ без сырого payload."""
    cid = uuid.uuid4().hex[:12]
    logger.warning("trello response invalid [ref=%s]: %s", cid, context)
    raise TrelloError(cid)


def _parse_action_date(action: dict[str, Any]) -> datetime:
    """Парсит aware timestamp action; невалидное время блокирует аудит."""
    raw_date = action.get("date")
    if not isinstance(raw_date, str) or not raw_date:
        _raise_trello_protocol_error("action_date_missing")
    try:
        action_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
    except ValueError:
        _raise_trello_protocol_error("action_date_invalid")
    if action_date.tzinfo is None:
        _raise_trello_protocol_error("action_date_naive")
    return action_date


def _validate_update_card_action(action: Any) -> tuple[str, datetime]:
    """Проверяет минимальную exact identity action до любой фильтрации."""
    if not isinstance(action, dict):
        _raise_trello_protocol_error("action_not_object")
    action_id = action.get("id")
    if not isinstance(action_id, str) or not action_id:
        _raise_trello_protocol_error("action_id_missing")
    if action.get("type") != "updateCard":
        _raise_trello_protocol_error("action_type_mismatch")
    data = action.get("data")
    card = data.get("card") if isinstance(data, dict) else None
    card_id = card.get("id") if isinstance(card, dict) else None
    if not isinstance(card_id, str) or not card_id:
        _raise_trello_protocol_error("action_card_id_missing")
    return action_id, _parse_action_date(action)


def is_due_complete_transition(action: dict[str, Any]) -> bool:
    """Проверяет exact переход dueComplete false -> true локально."""
    data = action.get("data")
    if not isinstance(data, dict):
        return False
    old = data.get("old")
    card = data.get("card")
    return (
        isinstance(old, dict)
        and isinstance(card, dict)
        and old.get("dueComplete") is False
        and card.get("dueComplete") is True
    )


def filter_due_complete_transitions(
    actions: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Оставляет только локально доказанные completion transitions."""
    return [action for action in actions if is_due_complete_transition(action)]


def get_board_update_card_actions(
    board_id: str,
    since: datetime,
    *,
    before: datetime | None = None,
    page_size: int = 1000,
) -> list[dict[str, Any]]:
    """Читает все страницы updateCard и возвращает completion transitions.

    API получает только documented ``filter=updateCard``. Проверка конкретного
    изменения dueComplete выполняется локально, поэтому subtype-фильтры Trello
    не используются. Граница ``since`` включительная.
    """
    if not isinstance(board_id, str) or not board_id.strip():
        raise ValueError("board_id должен быть непустой строкой")
    if not isinstance(since, datetime) or since.tzinfo is None:
        raise ValueError("since должен быть aware datetime")
    if before is not None and (
        not isinstance(before, datetime) or before.tzinfo is None
    ):
        raise ValueError("before должен быть aware datetime")
    if before is not None and before < since:
        raise ValueError("before должен быть не раньше since")
    if type(page_size) is not int or not 1 <= page_size <= 1000:
        raise ValueError("page_size должен быть от 1 до 1000")

    actions: list[dict[str, Any]] = []
    seen_actions: dict[str, dict[str, Any]] = {}
    cursor_before: str | None = None
    previous_oldest_date: datetime | None = None
    for _page in range(_MAX_ACTION_PAGES):
        params: dict[str, Any] = {
            "filter": "updateCard",
            "since": since.isoformat(),
            "limit": page_size,
        }
        if cursor_before is not None:
            params["before"] = cursor_before
        elif before is not None:
            # Trello трактует before как exclusive. Минимальный сдвиг и
            # локальный inclusive-фильтр сохраняют action ровно на границе.
            params["before"] = (before + timedelta(microseconds=1)).isoformat()
        response = safe_request(
            "GET",
            f"{BASE}/boards/{board_id}/actions",
            params=params,
        )
        try:
            page_actions = response.json()
        except ValueError:
            _raise_trello_protocol_error("actions_json_invalid")
        if not isinstance(page_actions, list):
            _raise_trello_protocol_error("actions_payload_not_list")
        if not page_actions:
            break

        new_ids_on_page = 0
        page_dates: list[datetime] = []
        for action in page_actions:
            action_id, action_date = _validate_update_card_action(action)
            page_dates.append(action_date)
            previous_action = seen_actions.get(action_id)
            if previous_action is not None:
                if previous_action != action:
                    _raise_trello_protocol_error("action_duplicate_conflict")
                continue
            seen_actions[action_id] = action
            new_ids_on_page += 1
            if action_date < since or (before is not None and action_date > before):
                continue
            actions.append(action)

        if new_ids_on_page == 0:
            _raise_trello_protocol_error("actions_pagination_not_advancing")
        if any(
            newer < older for newer, older in zip(page_dates, page_dates[1:])
        ):
            _raise_trello_protocol_error("actions_page_order_invalid")
        newest_date = page_dates[0]
        oldest_date = page_dates[-1]
        if previous_oldest_date is not None and newest_date > previous_oldest_date:
            _raise_trello_protocol_error("actions_cursor_window_drifted")
        next_cursor = page_actions[-1].get("id")
        if not isinstance(next_cursor, str) or not next_cursor:
            _raise_trello_protocol_error("actions_cursor_missing")
        if next_cursor == cursor_before:
            _raise_trello_protocol_error("actions_pagination_not_advancing")
        cursor_before = next_cursor
        previous_oldest_date = oldest_date
    else:
        _raise_trello_protocol_error("actions_page_limit_exceeded")

    return filter_due_complete_transitions(actions)


def get_card(card_id: str) -> dict[str, Any]:
    """Читает exact карточку recovery без каких-либо Trello mutations."""
    if not isinstance(card_id, str) or not card_id.strip():
        raise ValueError("card_id должен быть непустой строкой")
    response = safe_request(
        "GET",
        f"{BASE}/cards/{card_id}",
        params={
            "fields": (
                "id,name,desc,due,dueComplete,idList,labels,url,shortLink,closed"
            )
        },
    )
    try:
        card = response.json()
    except ValueError:
        _raise_trello_protocol_error("card_json_invalid")
    if not isinstance(card, dict):
        _raise_trello_protocol_error("card_not_object")
    live_card_id = card.get("id")
    if not isinstance(live_card_id, str) or live_card_id != card_id:
        _raise_trello_protocol_error("card_identity_mismatch")
    if (
        not isinstance(card.get("name"), str)
        or not isinstance(card.get("desc"), str)
        or type(card.get("dueComplete")) is not bool
        or not isinstance(card.get("idList"), str)
        or not card["idList"]
        or not isinstance(card.get("labels"), list)
        or type(card.get("closed")) is not bool
    ):
        _raise_trello_protocol_error("card_fields_invalid")
    return card


@dataclass(frozen=True, slots=True)
class TrelloCardSnapshot:
    """Полный force-live снимок карточки для approval checker."""

    card_id: str
    board_id: str
    list_id: str
    name: str
    description: str
    due: str | None
    due_complete: bool
    closed: bool
    date_last_activity: datetime
    labels: tuple[dict[str, Any], ...]
    attachments: tuple[dict[str, Any], ...]
    content_sha256: str
    # Отпечаток по старой схеме (с dueComplete и dateLastActivity) — для манифестов, собранных
    # до смены схемы; новые манифесты подписываются content_sha256 без этих полей.
    legacy_content_sha256: str = ""


def _require_snapshot_text(payload: dict[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        _raise_trello_protocol_error(f"snapshot_{field_name}_invalid")
    return value


def _canonical_snapshot_sha256(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        _raise_trello_protocol_error("snapshot_canonical_json_invalid")
    return hashlib.sha256(encoded).hexdigest()


def get_card_snapshot(card_id: str) -> TrelloCardSnapshot:
    """Читает exact card/list/attachments одним force-live Trello GET.

    Старый ``get_card`` намеренно не расширяется: recovery-контракт остаётся
    совместимым, а checker получает отдельную строгую границу.
    """

    if (
        not isinstance(card_id, str)
        or not card_id
        or _TRELLO_OBJECT_ID_RE.fullmatch(card_id) is None
    ):
        raise ValueError("card_id должен быть безопасным Trello ID")
    response = safe_request(
        "GET",
        f"{BASE}/cards/{card_id}",
        params={
            "fields": (
                "id,idBoard,idList,name,desc,due,dueComplete,closed,"
                "dateLastActivity,labels"
            ),
            "attachments": "true",
            "attachment_fields": "id,name,url,mimeType,bytes,date",
        },
    )
    try:
        payload = response.json()
    except ValueError:
        _raise_trello_protocol_error("snapshot_json_invalid")
    if not isinstance(payload, dict):
        _raise_trello_protocol_error("snapshot_not_object")
    if payload.get("id") != card_id:
        _raise_trello_protocol_error("snapshot_identity_mismatch")

    board_id = _require_snapshot_text(payload, "idBoard")
    list_id = _require_snapshot_text(payload, "idList")
    name = _require_snapshot_text(payload, "name")
    description = payload.get("desc")
    if not isinstance(description, str):
        _raise_trello_protocol_error("snapshot_desc_invalid")
    due = payload.get("due")
    if due is not None and not isinstance(due, str):
        _raise_trello_protocol_error("snapshot_due_invalid")
    due_complete = payload.get("dueComplete")
    closed = payload.get("closed")
    if type(due_complete) is not bool or type(closed) is not bool:
        _raise_trello_protocol_error("snapshot_state_invalid")
    raw_activity = _require_snapshot_text(payload, "dateLastActivity")
    try:
        date_last_activity = datetime.fromisoformat(
            raw_activity.replace("Z", "+00:00")
        )
    except ValueError:
        _raise_trello_protocol_error("snapshot_activity_invalid")
    if date_last_activity.tzinfo is None or date_last_activity.utcoffset() is None:
        _raise_trello_protocol_error("snapshot_activity_naive")

    raw_labels = payload.get("labels")
    raw_attachments = payload.get("attachments")
    if not isinstance(raw_labels, list) or not isinstance(raw_attachments, list):
        _raise_trello_protocol_error("snapshot_collections_invalid")
    labels: list[dict[str, Any]] = []
    seen_label_ids: set[str] = set()
    for raw_label in raw_labels:
        if not isinstance(raw_label, dict):
            _raise_trello_protocol_error("snapshot_label_invalid")
        label_id = _require_snapshot_text(raw_label, "id")
        if label_id in seen_label_ids:
            _raise_trello_protocol_error("snapshot_label_duplicate")
        seen_label_ids.add(label_id)
        label_name = raw_label.get("name")
        label_color = raw_label.get("color")
        if not isinstance(label_name, str) or (
            label_color is not None and not isinstance(label_color, str)
        ):
            _raise_trello_protocol_error("snapshot_label_fields_invalid")
        labels.append({"id": label_id, "name": label_name, "color": label_color})

    attachments: list[dict[str, Any]] = []
    seen_attachment_ids: set[str] = set()
    for raw_attachment in raw_attachments:
        if not isinstance(raw_attachment, dict):
            _raise_trello_protocol_error("snapshot_attachment_invalid")
        attachment_id = _require_snapshot_text(raw_attachment, "id")
        if attachment_id in seen_attachment_ids:
            _raise_trello_protocol_error("snapshot_attachment_duplicate")
        seen_attachment_ids.add(attachment_id)
        attachment_name = raw_attachment.get("name")
        attachment_url = raw_attachment.get("url")
        mime_type = raw_attachment.get("mimeType")
        size_bytes = raw_attachment.get("bytes")
        attachment_date = raw_attachment.get("date")
        if (
            not isinstance(attachment_name, str)
            or not isinstance(attachment_url, str)
            or not attachment_url
            or (mime_type is not None and not isinstance(mime_type, str))
            or (
                size_bytes is not None
                and (
                    isinstance(size_bytes, bool)
                    or not isinstance(size_bytes, int)
                    or size_bytes < 0
                )
            )
            or (attachment_date is not None and not isinstance(attachment_date, str))
        ):
            _raise_trello_protocol_error("snapshot_attachment_fields_invalid")
        attachments.append(
            {
                "id": attachment_id,
                "name": attachment_name,
                "url": attachment_url,
                "mimeType": mime_type,
                "bytes": size_bytes,
                "date": attachment_date,
            }
        )

    # Галочка (dueComplete) и дата активности в отпечаток НЕ входят: галочку ставит сверщик
    # после первого живого объявления, и она не должна ломать дозапуск остальных городов
    # (иначе тихий 48-часовой цикл LIVE_EVIDENCE_INCOMPLETE_TRELLO). Содержимое
    # карточки — название, текст, срок, метки, вложения, список, closed — по-прежнему строгое.
    content = {
        "id": card_id,
        "idBoard": board_id,
        "idList": list_id,
        "name": name,
        "desc": description,
        "due": due,
        "closed": closed,
        "labels": labels,
        "attachments": attachments,
    }
    legacy_content = {
        **content,
        "dueComplete": due_complete,
        "dateLastActivity": date_last_activity.isoformat(),
    }
    return TrelloCardSnapshot(
        card_id=card_id,
        board_id=board_id,
        list_id=list_id,
        name=name,
        description=description,
        due=due,
        due_complete=due_complete,
        closed=closed,
        date_last_activity=date_last_activity,
        labels=tuple(labels),
        attachments=tuple(attachments),
        content_sha256=_canonical_snapshot_sha256(content),
        legacy_content_sha256=_canonical_snapshot_sha256(legacy_content),
    )


@lru_cache(maxsize=1)
def get_done_list_id() -> str:
    """Возвращает ID колонки 'Готово'. Кешируется — ID не меняется."""
    resp = session.get(f"{BASE}/boards/{TRELLO_BOARD_ID}/lists")
    resp.raise_for_status()
    for lst in resp.json():
        if lst["name"] == TRELLO_DONE_LIST_NAME:
            return lst["id"]
    raise ValueError(f"Колонка '{TRELLO_DONE_LIST_NAME}' не найдена")


def get_unlaunched_cards(list_id: str) -> list[dict]:
    """
    Возвращает открытые карточки без зелёного чекбокса сверху вниз.
    Незапущенная = dueComplete = False и closed = False.
    Поле labels — список имён меток (или цвет если имя пустое).

    Снимок Trello проверяется целиком до фильтрации: битая карточка не должна
    приводить к частичному и потенциально неверному порядку запуска.
    """
    return [card for card in get_open_cards(list_id) if card["dueComplete"] is False]


def get_open_cards(list_id: str) -> list[dict]:
    """
    Возвращает ВСЕ открытые карточки колонки сверху вниз — с галочкой и без.
    Нужна там, где карточку ищут по id/имени независимо от отметки «запущено»
    (долив недостающих городов в scripts/manual_launch.py): get_unlaunched_cards
    отмеченную карточку не видит, и повторный прогон её «не находил».

    Валидация снимка и нормализация labels — те же, что у get_unlaunched_cards.
    """
    resp = session.get(
        f"{BASE}/lists/{list_id}/cards",
        params={
            "fields": (
                "id,name,desc,dueComplete,due,idAttachmentCover,labels,"
                "pos,closed,shortLink"
            )
        },
    )
    resp.raise_for_status()
    try:
        cards = resp.json()
    except ValueError:
        _raise_trello_protocol_error("cards_json_invalid")
    if not isinstance(cards, list):
        _raise_trello_protocol_error("cards_payload_not_list")

    validated_cards: list[dict] = []
    for card in cards:
        if not isinstance(card, dict):
            _raise_trello_protocol_error("card_not_object")
        card_id = card.get("id")
        card_name = card.get("name")
        card_pos = card.get("pos")
        if not isinstance(card_id, str) or not card_id:
            _raise_trello_protocol_error("card_id_missing")
        if not isinstance(card_name, str) or not card_name:
            _raise_trello_protocol_error("card_name_missing")
        if (
            isinstance(card_pos, bool)
            or not isinstance(card_pos, (int, float))
            or not math.isfinite(float(card_pos))
        ):
            _raise_trello_protocol_error("card_pos_invalid")
        if type(card.get("dueComplete")) is not bool:
            _raise_trello_protocol_error("card_due_complete_invalid")
        if type(card.get("closed")) is not bool:
            _raise_trello_protocol_error("card_closed_invalid")

        normalized_card = dict(card)
        normalized_card["pos"] = float(card_pos)
        validated_cards.append(normalized_card)

    result: list[dict] = []
    for card in validated_cards:
        if card["closed"] is not False:
            continue
        # Извлекаем имена меток (fallback на цвет если имя пустое)
        label_names = [
            lbl.get("name") or lbl.get("color", "")
            for lbl in card.get("labels", [])
            if isinstance(lbl, dict) and (lbl.get("name") or lbl.get("color"))
        ]
        card["labels"] = label_names
        result.append(card)
    return sorted(result, key=lambda card: (card["pos"], card["id"]))


def get_card_drive_link(card_id: str) -> str | None:
    """Возвращает первую Google Drive ссылку из вложений карточки."""
    resp = session.get(f"{BASE}/cards/{card_id}/attachments")
    resp.raise_for_status()
    for att in resp.json():
        url = att.get("url", "")
        if "drive.google.com" in url:
            return url
    return None


def get_card_body(card_desc: str) -> str:
    """Извлекает тело объявления из описания карточки (всё после ссылки на инстаграм)."""
    lines = card_desc.split("\n")
    body_lines = []
    collecting = False
    for line in lines:
        if "instagram.com" in line or "instagram" in line.lower():
            collecting = True
            continue
        if collecting and line.strip():
            body_lines.append(line.strip())
    return " ".join(body_lines) if body_lines else card_desc


# detect_language перенесена в services/language.py (обратная совместимость)
from services.language import detect_language  # noqa: F401, E402


def mark_card_done(card_id: str):
    """Отмечает карточку как выполненную (зелёный чекбокс)."""
    resp = session.put(
        f"{BASE}/cards/{card_id}",
        params={"dueComplete": "true"},
    )
    resp.raise_for_status()


# --- Гипотезы ---

HYPOTHESES_LIST_NAME = "Гипотезы"

# Цвета меток по типу гипотезы
LABEL_COLORS = {
    "scale": "green",
    "experiment": "blue",
    "reduce": "red",
    "research": "yellow",
}


def get_or_create_list(list_name: str) -> str:
    """Находит колонку по имени или создаёт новую. Возвращает list_id."""
    resp = session.get(f"{BASE}/boards/{TRELLO_BOARD_ID}/lists")
    resp.raise_for_status()
    for lst in resp.json():
        if lst["name"] == list_name:
            return lst["id"]

    # Создаём колонку в конце доски
    resp = session.post(
        f"{BASE}/boards/{TRELLO_BOARD_ID}/lists",
        data={"name": list_name, "pos": "bottom"},
    )
    resp.raise_for_status()
    return resp.json()["id"]


def create_hypothesis_card(list_id: str, hypothesis: dict) -> dict:
    """Создаёт карточку-гипотезу в Trello.
    hypothesis: {title, description, type, priority, ads}
    Возвращает данные созданной карточки."""
    # Формируем описание
    desc_parts = []
    if hypothesis.get("description"):
        desc_parts.append(hypothesis["description"])
    if hypothesis.get("ads"):
        desc_parts.append("\n**Объявления:**")
        for ad in hypothesis["ads"][:5]:  # Макс 5 объявлений
            desc_parts.append(f"- {ad}")
    desc_parts.append(f"\n_Тип: {hypothesis.get('type', '?')} | Приоритет: {hypothesis.get('priority', '?')}_")

    resp = session.post(
        f"{BASE}/cards",
        data={
            "idList": list_id,
            "name": hypothesis["title"],
            "desc": "\n".join(desc_parts),
            "pos": "top" if hypothesis.get("priority") == "high" else "bottom",
        },
    )
    resp.raise_for_status()
    return resp.json()


def publish_hypotheses(hypotheses: list[dict]) -> list[dict]:
    """Публикует список гипотез в Trello колонку 'Гипотезы'.
    Возвращает список созданных карточек."""
    if not hypotheses:
        return []

    list_id = get_or_create_list(HYPOTHESES_LIST_NAME)
    created = []
    for h in hypotheses:
        card = create_hypothesis_card(list_id, h)
        created.append({"id": card["id"], "title": h["title"], "url": card.get("url", "")})
    return created


# --- ТЗ (авто-брифы) ---

# Имя колонки для авто-ТЗ — отдельная от готовых-к-запуску, чтобы авто-запуск не схватил
BRIEFS_LIST_NAME = "Идеи / ТЗ"

# Сценарист v2 (ARCH-phase3-scenarist.md): карточки уходят на ручную проверку
# владельца, НЕ в "Готово" — auto_launch читает только get_done_list_id()
# (TRELLO_DONE_LIST_NAME == "Готово"), поэтому "На проверку" им не подхватывается.
# REVIEW_LIST_NAME больше не используется генератором (ARCH-brief-approval-flow.md):
# ТЗ теперь сначала уходит владельцу в Telegram на одобрение, карточка создаётся
# только после клика "Одобрить" — сразу в BRIEF_LIST_NAME. Константа оставлена
# (может использоваться другим кодом / для обратной совместимости).
REVIEW_LIST_NAME = "На проверку"

# Колонка "ТЗ реклам" — сюда после одобрения владельцем в Telegram создаётся
# карточка ТЗ (services/telegram_bot.py::_execute_approve_brief). Точное имя
# колонки борда проверено read-only-запросом к Trello API (GET /1/boards/.../lists,
# без мутаций) — колонка уже существует, get_or_create_list её найдёт по точному
# имени и ничего не создаст.
BRIEF_LIST_NAME = "ТЗ реклам"


# --- Продуктовые метки на карточках ТЗ (ARCH-product-tags.md) ---
#
# Сценарист (services/brief_generator.py) при создании карточки ТЗ уже знает
# продукт (target_product победителя-референса / keyword-эвристика / LLM-добор,
# см. services/product_tags.py) и вешает Trello-метку продукта СРАЗУ при
# создании карточки — отдельным проходом по уже созданным карточкам метки
# не расставляются (см. §8 спеки: "владелец/сценарист вешает метку Trello").
# Позже launcher/auto_launch читают эту метку через classify_product
# (приоритет 1 — Trello-label) при запуске рекламы.

# Цвета Trello-меток по продукту (аналогично LABEL_COLORS для гипотез выше).
# Имена продуктов здесь не хардкодятся: цвет берётся по порядку реестра
# services/product_tags.PRODUCTS (единая точка правды), дефолтный продукт —
# чёрный, как и продукт вне реестра.
_PRODUCT_COLOR_PALETTE: tuple[str, ...] = ("purple", "sky", "lime", "orange", "pink", "green")
_DEFAULT_PRODUCT_COLOR = "black"


def _product_label_color(product: str) -> str:
    """Цвет Trello-метки продукта по его позиции в реестре продуктов."""
    from services.product_tags import DEFAULT_PRODUCT, PRODUCTS

    if product == DEFAULT_PRODUCT:
        return _DEFAULT_PRODUCT_COLOR
    ordered = [name for name in PRODUCTS if name != DEFAULT_PRODUCT]
    if product not in ordered:
        return _DEFAULT_PRODUCT_COLOR
    return _PRODUCT_COLOR_PALETTE[ordered.index(product) % len(_PRODUCT_COLOR_PALETTE)]


# Простой кеш "продукт -> id метки на доске" на время процесса — чтобы не
# опрашивать список меток доски перед КАЖДОЙ карточкой (продуктов немного,
# они не меняются в рамках рантайма). Не lru_cache — так проще сбрасывать
# в тестах (_product_label_cache.clear()).
_product_label_cache: dict[str, str] = {}


def _get_or_create_product_label(product: str) -> str | None:
    """Находит Trello-метку продукта на доске по имени или создаёт новую.

    Fail-safe: любая ошибка Trello API -> None (карточка создаётся БЕЗ метки,
    create_card не падает, см. create_card).
    """
    if product in _product_label_cache:
        return _product_label_cache[product]

    try:
        resp = session.get(f"{BASE}/boards/{TRELLO_BOARD_ID}/labels")
        resp.raise_for_status()
        for lbl in resp.json():
            if lbl.get("name") == product:
                _product_label_cache[product] = lbl["id"]
                return lbl["id"]

        color = _product_label_color(product)
        resp = session.post(
            f"{BASE}/boards/{TRELLO_BOARD_ID}/labels",
            data={"name": product, "color": color},
        )
        resp.raise_for_status()
        label_id = resp.json()["id"]
        _product_label_cache[product] = label_id
        return label_id
    except Exception as exc:
        logger.warning("trello: не удалось получить/создать метку продукта '%s' — %s", product, exc)
        return None


def create_card(list_id: str, name: str, desc: str, product: str | None = None) -> dict:
    """Создаёт карточку в Trello.

    Args:
        list_id: ID колонки
        name: название карточки
        desc: описание карточки (markdown)
        product: продукт из реестра services/product_tags.PRODUCTS — если задан, карточка сразу
            получает Trello-метку продукта (метка создаётся на доске при
            отсутствии). Метка вешается ОДНИМ запросом создания карточки
            (idLabels в том же POST) — отдельного запроса на добавление метки
            к уже созданной карточке нет. Fail-safe: если метку не удалось
            получить/создать — карточка всё равно создаётся, просто без
            метки (лог warning), create_card не падает из-за этого.

    Returns:
        Данные созданной карточки (id, url, ...)

    Raises:
        requests.HTTPError: ошибка Trello API создания карточки
    """
    data = {
        "idList": list_id,
        "name": name,
        "desc": desc,
        "pos": "top",
    }
    if product:
        label_id = _get_or_create_product_label(product)
        if label_id:
            data["idLabels"] = label_id
        else:
            logger.warning("trello: карточка '%s' создаётся БЕЗ метки продукта '%s'", name, product)

    resp = session.post(f"{BASE}/cards", data=data)
    resp.raise_for_status()
    return resp.json()
