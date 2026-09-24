"""Страж повторных заявок — возвращает лид менеджеру после сброса fblead'ом.

Интеграция fblead.com (FB-лиды → AMO) при повторной заявке клиента не создаёт
нового лида, а пишет заявку в существующий активный лид «Новых продаж»:
добавляет заметку «ПОВТОРНАЯ ЗАЯВКА … / Источник: fblead.com», перезаписывает
FB-поля и сбрасывает ответственного на псевдо-юзера «Биржа лидов». Менеджер
теряет лид, тот уходит в общую раздачу.

Страж по крону ищет в событиях AMO сбросы «менеджер → Биржа» от интеграции
(created_by=0) с fblead-заметкой о повторной заявке рядом по времени и:
  - возвращает исходного ответственного — безусловно, даже если распределитель
    уже успел отдать лид другому (решение владельца);
  - ставит исходному менеджеру задачу «повторная заявка» (просьба ОП), если
    это живой менеджер, а не псевдо-юзер онлайн-очереди.

Те же события с created_by=0 дают два штатных потока, которые трогать нельзя:
  - транзит новорождённого лида (fblead создал → на биржу → раздача),
    его заметка — «НОВАЯ СДЕЛКА»;
  - SLA-возврат «в общий пул» при простое менеджера.
Оба отсеиваются требованием fblead-заметки «ПОВТОРНАЯ ЗАЯВКА» возле сброса.

Исходящие вебхуки AMO в этом аккаунте не доставляются
— поэтому серверный опрос событий,
как у scripts/backfill_lead_source.py. Запуск: scripts/run_repeat_lead_guard.py.
"""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from integrations.amo import _amo_get, _amo_patch, _amo_post
from services.state_store import load_json_state, save_json_state

log = logging.getLogger(__name__)

BIRZHA_USER_ID = 2801893         # псевдо-юзер «Биржа лидов» — нераспределённые
ACME_ONLINE_USER_ID = 8572867  # системный распределитель онлайн-потока
PSEUDO_USERS = {BIRZHA_USER_ID, ACME_ONLINE_USER_ID}

PIPELINE_NOVYE_PRODAZHI = 3480844  # fblead работает только с целевой воронкой

# Оба варианта заметки fblead начинаются одинаково:
# «ПОВТОРНАЯ ЗАЯВКА В ЦЕЛЕВОЙ ВОРОНКЕ» и «ПОВТОРНАЯ ЗАЯВКА НА ЦЕЛЕВОМ ЭТАПЕ»
REPEAT_NOTE_PREFIX = "ПОВТОРНАЯ ЗАЯВКА"
REPEAT_NOTE_SOURCE = "fblead.com"
NOTE_MATCH_WINDOW_SEC = 300  # заметка и сброс приходят одной пачкой секунд

CLOSED_STATUS_IDS = {142, 143}  # закрытые статусы воронки (status.type не годится)

TASK_TYPE_CONTACT = 1  # тип задачи AMO «Связаться»
TASK_DEADLINE_HOURS = 3
TASK_TEXT = "Повторная заявка: клиент снова оставил заявку (FB). Связаться повторно."

STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "repeat_lead_guard_state.json"
STATE_TTL_DAYS = 7

EVENTS_PAGE_LIMIT = 100
EVENTS_MAX_PAGES = 10


def _flip_from_event(event: dict) -> dict | None:
    """Событие → сброс «менеджер → Биржа» интеграцией, иначе None.

    Ручные переводы (created_by = живой юзер) и раздача биржей (before=Биржа)
    отсеиваются здесь; штатные потоки интеграций — позже, по заметкам лида.
    """
    if event.get("created_by") != 0:
        return None
    try:
        after = event["value_after"][0]["responsible_user"]["id"]
        before = event["value_before"][0]["responsible_user"]["id"]
    except (KeyError, IndexError, TypeError):
        return None
    if after != BIRZHA_USER_ID or before in (None, BIRZHA_USER_ID):
        return None
    return {
        "lead_id": event.get("entity_id"),
        "flip_ts": event.get("created_at"),
        "prev_responsible_id": before,
    }


def fetch_responsible_flips(window_minutes: int) -> list[dict]:
    """Сбросы ответственного на «Биржу лидов» за окно, свежие первыми."""
    from_ts = int(time.time()) - window_minutes * 60
    flips = []
    for page in range(1, EVENTS_MAX_PAGES + 1):
        data = _amo_get(
            "events",
            {
                "filter[type]": "entity_responsible_changed",
                "filter[created_at][from]": from_ts,
                "limit": EVENTS_PAGE_LIMIT,
                "page": page,
            },
        )
        events = (data.get("_embedded") or {}).get("events") or []
        for event in events:
            flip = _flip_from_event(event)
            if flip and flip["lead_id"] and flip["flip_ts"]:
                flips.append(flip)
        if len(events) < EVENTS_PAGE_LIMIT:
            break
    return flips


def _fetch_lead(lead_id: int) -> dict | None:
    """Лид либо None. Сбросы на биржу случаются и в чужих воронках, а их лиды
    часто уже удалены — AMO отвечает 204 без тела, это не ошибка стража."""
    data = _amo_get(f"leads/{lead_id}")
    return data if data.get("id") else None


def _fetch_lead_notes(lead_id: int) -> list[dict]:
    data = _amo_get(f"leads/{lead_id}/notes", {"limit": 50})
    return (data.get("_embedded") or {}).get("notes") or []


def has_repeat_note(notes: list[dict], flip_ts: int) -> bool:
    """Есть ли рядом со сбросом заметка fblead о повторной заявке."""
    for note in notes:
        text = ((note.get("params") or {}).get("text") or "")
        if not text.startswith(REPEAT_NOTE_PREFIX):
            continue
        if REPEAT_NOTE_SOURCE not in text:
            continue
        if abs((note.get("created_at") or 0) - flip_ts) <= NOTE_MATCH_WINDOW_SEC:
            return True
    return False


def plan_action(flip: dict, lead: dict | None, notes: list[dict]) -> dict:
    """Решение по одному сбросу. Чистая функция — вся политика здесь.

    Returns:
        {"decision": "skip", "reason": ...} либо
        {"decision": "restore", "need_patch": bool, "task_user_id": int | None}
    """
    if not lead:
        return {"decision": "skip", "reason": "lead_missing"}
    if lead.get("pipeline_id") not in (None, PIPELINE_NOVYE_PRODAZHI):
        return {"decision": "skip", "reason": "foreign_pipeline"}
    if lead.get("status_id") in CLOSED_STATUS_IDS:
        return {"decision": "skip", "reason": "lead_closed"}
    if not has_repeat_note(notes, flip["flip_ts"]):
        # SLA-возврат «в общий пул» или транзит новорождённого лида — не трогаем
        return {"decision": "skip", "reason": "no_repeat_note"}
    prev = flip["prev_responsible_id"]
    return {
        "decision": "restore",
        "reason": "fblead_repeat",
        "need_patch": lead.get("responsible_user_id") != prev,
        "task_user_id": prev if prev not in PSEUDO_USERS else None,
    }


def _create_repeat_task(lead_id: int, user_id: int) -> None:
    _amo_post(
        "tasks",
        [
            {
                "entity_id": lead_id,
                "entity_type": "leads",
                "responsible_user_id": user_id,
                "task_type_id": TASK_TYPE_CONTACT,
                "text": TASK_TEXT,
                "complete_till": int(time.time()) + TASK_DEADLINE_HOURS * 3600,
            }
        ],
    )


def _state_key(flip: dict) -> str:
    return f"{flip['lead_id']}:{flip['flip_ts']}"


def _prune_processed(processed: dict) -> dict:
    """Выкидывает записи старше STATE_TTL_DAYS — ключ несёт ts сброса."""
    cutoff = int(time.time()) - STATE_TTL_DAYS * 86400
    kept = {}
    for key, value in processed.items():
        try:
            flip_ts = int(key.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        if flip_ts >= cutoff:
            kept[key] = value
    return kept


def run(window_minutes: int = 45, apply: bool = False, limit: int = 30) -> dict:
    """Основной проход. Без apply — dry-run: только лог, ни записи, ни state."""
    state = load_json_state(STATE_FILE)
    processed = _prune_processed(state.get("processed") or {})
    stats = {"flips": 0, "restored": 0, "tasks": 0, "skipped": {}, "errors": 0}

    flips = fetch_responsible_flips(window_minutes)
    stats["flips"] = len(flips)

    # На лид — одно действие за прогон, по самому свежему сбросу. Порядок ответа
    # API не гарантирован — сортируем сами. Старые сбросы помечаем обработанными.
    flips.sort(key=lambda f: f["flip_ts"], reverse=True)
    fresh_by_lead: dict[int, dict] = {}
    superseded: list[str] = []
    for flip in flips:
        if _state_key(flip) in processed:
            continue
        if flip["lead_id"] in fresh_by_lead:
            superseded.append(_state_key(flip))
            continue
        fresh_by_lead[flip["lead_id"]] = flip

    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    actions = 0
    for lead_id, flip in fresh_by_lead.items():
        if actions >= limit:
            log.warning("Достигнут лимит %d действий за прогон — останавливаюсь", limit)
            break
        try:
            lead = _fetch_lead(lead_id)
            notes = _fetch_lead_notes(lead_id) if lead else []
        except Exception as e:
            log.warning("Лид %s: не удалось прочитать состояние: %s", lead_id, e)
            stats["errors"] += 1
            continue

        plan = plan_action(flip, lead, notes)
        prev = flip["prev_responsible_id"]

        if plan["decision"] == "skip":
            stats["skipped"][plan["reason"]] = stats["skipped"].get(plan["reason"], 0) + 1
            processed[_state_key(flip)] = now_iso
            continue

        actions += 1
        if not apply:
            log.info(
                "[dry-run] лид %s: вернул бы ответственного %s%s%s",
                lead_id,
                prev,
                "" if plan["need_patch"] else " (уже вернулся сам)",
                f" + задача менеджеру {plan['task_user_id']}" if plan["task_user_id"] else "",
            )
            continue

        try:
            if plan["need_patch"]:
                _amo_patch(f"leads/{lead_id}", {"responsible_user_id": prev})
                stats["restored"] += 1
            if plan["task_user_id"]:
                _create_repeat_task(lead_id, plan["task_user_id"])
                stats["tasks"] += 1
            processed[_state_key(flip)] = now_iso
            log.info(
                "лид %s: ответственный возвращён → %s (patch=%s, задача=%s)",
                lead_id, prev, plan["need_patch"], bool(plan["task_user_id"]),
            )
        except Exception as e:
            # PATCH мог пройти до падения задачи — state не пишем, следующий
            # прогон увидит need_patch=False и доставит только задачу
            log.warning("Лид %s: возврат не удался: %s", lead_id, e)
            stats["errors"] += 1

    if apply:
        for key in superseded:
            processed[key] = now_iso
        save_json_state(STATE_FILE, {"processed": processed, "last_run": now_iso})

    return stats
