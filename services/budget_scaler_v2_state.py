"""Состояние Budget Scaler v2 (per-adset) — кулдаун, baseline, детект ручной
правки владельца, hold, undo-карта возврата бюджета.

Модуль изолированный: используется этапами 2-3 лестницы v2 (подъём/снижение),
которые включатся отдельным релизом. Сейчас — только модуль + тесты.

Конкурентность (Wave 4, по образцу services/budget_daily_cap.py):
- Все read-modify-write JSON выполняются под меж-процессным flock (стабильный
  .lock-файл рядом со state, НЕ на самом JSON, который заменяется через rename)
  плюс внутри-процессный threading-lock. Так два процесса, меняющие РАЗНЫЕ
  адсеты, не затирают правки друг друга (второй перечитывает свежий файл).
- Запись атомарна: уникальный temp + fsync + os.replace.
- Битый/нечитаемый state в mutation-пути → fail-closed: НЕ перезаписываем его
  пустым дефолтом (иначе потеряли бы hold/undo/manual-edit). Гейты решений
  (in_adset_cooldown / is_on_hold) при битом state отвечают «блокировано».
- baseline фиксируется только из валидного (finite, положительного) FB-бюджета.
- prune реально вызывается из write-пути и ограничивает undo/adset-записи по
  возрасту И размеру, не удаляя активный hold / незакрытый undo / свежую
  ручную правку.

State-файл: data/budget_scaler_v2_state.json
Формат:
{
  "adsets": {
      "<adset_id>": {
          "last_raise_at": "2026-07-16T13:00:00+05:00" | null,
          "last_decrease_at": "..." | null,
          "last_set_at": "..." | null,       # когда бот сам выставил бюджет
          "last_set_budget_usd": 115.0 | null,  # что именно выставил (для детекта ручной правки)
          "baseline_budget_usd": 100.0 | null,  # опорный бюджет (пол снижения считается от него)
          "hold_until": "..." | null          # адсет «заморожен» до этой даты (бот не трогает)
      }
  },
  "undo": {
      "<adset_id>": {
          "adset_id": "...",
          "prev_budget_usd": 100.0,   # что вернуть
          "new_budget_usd": 90.0,     # что было выставлено при снижении
          "at": "...",                # время создания записи (для ретеншна)
          "done": false               # уже вернули?
      }
  }
}
"""

import json
import logging
import math
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # fcntl есть на нашем прод-Linux; на Windows его нет — деградируем до threading-lock
    import fcntl
except ImportError:  # pragma: no cover — прод только Linux/macOS
    fcntl = None

logger = logging.getLogger(__name__)

# Часовой пояс CityA (UTC+5) — единый для всей группы модулей budget_*
_TZ_LOCAL = timezone(timedelta(hours=5))

# State-файл (по образцу budget_daily_cap_state.json / budget_scaler_state.json)
_V2_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "budget_scaler_v2_state.json"

# Ретеншн undo-карты «↩️ Вернуть бюджет» — зеркало UNDO_MAP автопилота
# (services.autopilot.UNDO_MAP_RETENTION_DAYS / MAX_UNDO_ENTRIES).
UNDO_RETENTION_DAYS = 30
MAX_UNDO_ENTRIES = 200

# Ретеншн неактивных adset-записей (нет hold и все отметки старше N дней) —
# чтобы файл не рос бесконечно. Консервативно большой.
ADSET_RETENTION_DAYS = 90
# Потолок числа adset-записей (защита от неограниченного роста файла).
MAX_ADSET_ENTRIES = 500

# Окно, внутри которого расхождение текущего бюджета с последним выставленным
# ботом считаем ручной правкой владельца (детект detect_manual_edit).
_MANUAL_EDIT_FRESH_HOURS = 48

# Внутрипроцессная защита (потоки одного процесса). RLock — на случай вложенного
# вызова публичной функции; flock при этом НЕ вкладываем (см. _v2_lock).
_V2_TLOCK = threading.RLock()


class V2StateError(Exception):
    """v2-state нечитаем/битой структуры — в mutation-пути это fail-closed сигнал."""


def _v2_lock_path() -> Path:
    """Стабильный .lock-файл рядом со state. Блокируем именно его, а не JSON:
    JSON заменяется через rename, и flock на заменяемом файле потерялся бы."""
    return _V2_STATE_FILE.with_name(_V2_STATE_FILE.name + ".lock")


@contextmanager
def _v2_lock():
    """Меж-процессный (flock) + внутри-процессный (RLock) замок v2-state.

    ВАЖНО: не вкладывать один _v2_lock() в другой в том же потоке — flock на
    втором fd того же файла в том же процессе заблокировал бы сам себя. Публичные
    функции берут замок ровно один раз и не вызывают друг друга под замком.
    """
    with _V2_TLOCK:
        if fcntl is None:  # pragma: no cover — платформа без fcntl
            yield
            return
        path = _v2_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # блокирующий: секции короткие
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _now_local(now: datetime | None = None) -> datetime:
    """Нормализует момент к aware-datetime TZ CityA. now=None → текущий момент.

    Naive считаем уже временем CityA; aware — приводим к CityA.
    """
    moment = now if now is not None else datetime.now(_TZ_LOCAL)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_TZ_LOCAL)
    return moment.astimezone(_TZ_LOCAL)


def _parse_iso(s) -> datetime | None:
    """ISO-строка → aware datetime (CityA). None если пусто/битое."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ_LOCAL)
    return dt


def _is_valid_budget(v) -> bool:
    """True если значение — конечное строго положительное число (валидный бюджет)."""
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(fv) and fv > 0


def _load_state() -> dict:
    """Толерантное чтение state (для read-only/мониторинга). При ошибке/отсутствии
    → {'adsets': {}, 'undo': {}}. Замок НЕ берёт.

    ВНИМАНИЕ: mutation-путь использует _load_state_strict (fail-closed), чтобы
    битый файл не был перезаписан пустым дефолтом.
    """
    default: dict = {"adsets": {}, "undo": {}}
    if not _V2_STATE_FILE.exists():
        return default
    try:
        data = json.loads(_V2_STATE_FILE.read_text(encoding="utf-8"))
        default.update(data)
        # Защита от битого формата вложенных словарей
        if not isinstance(default.get("adsets"), dict):
            default["adsets"] = {}
        if not isinstance(default.get("undo"), dict):
            default["undo"] = {}
        return default
    except Exception as exc:
        logger.warning("_load_state (scaler_v2): не удалось прочитать — %s", exc)
        return default


def _load_state_strict() -> dict:
    """Строгое чтение для mutation-пути: битый/нечитаемый файл или неверная
    структура → V2StateError (fail-closed, НЕ перезаписываем пустым). Замок НЕ берёт."""
    if not _V2_STATE_FILE.exists():
        return {"adsets": {}, "undo": {}}
    try:
        raw = _V2_STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        raise V2StateError(f"v2-state нечитаем: {exc}") from exc
    if not isinstance(data, dict):
        raise V2StateError("v2-state не dict")
    adsets = data.get("adsets", {})
    undo = data.get("undo", {})
    if not isinstance(adsets, dict):
        raise V2StateError("v2-state.adsets не dict")
    if not isinstance(undo, dict):
        raise V2StateError("v2-state.undo не dict")
    data["adsets"] = adsets
    data["undo"] = undo
    return data


def _save_state(state: dict) -> None:
    """Атомарно сохраняет state: уникальный temp + fsync + os.replace.

    Замок НЕ берёт — вызывающий держит _v2_lock(). Ошибку пробрасывает."""
    _V2_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _V2_STATE_FILE.with_name(
        f"{_V2_STATE_FILE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _V2_STATE_FILE)
    except Exception as exc:
        logger.error("_save_state (scaler_v2): не удалось сохранить — %s", exc)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


def _get_adset(state: dict, adset_id: str) -> dict:
    """Возвращает (создавая при отсутствии) запись адсета в state['adsets']."""
    adsets = state["adsets"]
    key = str(adset_id)
    if key not in adsets or not isinstance(adsets.get(key), dict):
        adsets[key] = {
            "last_raise_at": None,
            "last_decrease_at": None,
            "last_set_at": None,
            "last_set_budget_usd": None,
            "baseline_budget_usd": None,
            "hold_until": None,
        }
    return adsets[key]


# ---------------------------------------------------------------------------
# Кулдаун подъёма/снижения
# ---------------------------------------------------------------------------

def in_adset_cooldown(
    adset_id: str, kind: str, cooldown_hours: float, now: datetime | None = None
) -> tuple[bool, str | None]:
    """Действует ли кулдаун на подъём (kind='raise') или снижение (kind='decrease').

    Returns:
        (True, причина) если ещё рано трогать адсет, (False, None) если можно.
    Битый state → (True, ...) — fail-closed (гейт блокирует решение, а не открывает).
    """
    field = "last_raise_at" if kind == "raise" else "last_decrease_at"
    try:
        with _v2_lock():
            state = _load_state_strict()
    except V2StateError as exc:
        logger.error(
            "in_adset_cooldown: v2-state битой — fail-closed (кулдаун активен) adset=%s: %s",
            adset_id, exc,
        )
        return True, "v2-state недоступен — fail-closed"

    entry = state["adsets"].get(str(adset_id))
    if not isinstance(entry, dict):
        return False, None
    last_dt = _parse_iso(entry.get(field))
    if last_dt is None:
        return False, None
    now_dt = _now_local(now)
    elapsed_hours = (now_dt - last_dt).total_seconds() / 3600.0
    if elapsed_hours < cooldown_hours:
        minutes_left = int((cooldown_hours - elapsed_hours) * 60)
        word = "подъём" if kind == "raise" else "снижение"
        reason = f"кулдаун {word}: последнее {elapsed_hours:.1f}ч назад, ещё {minutes_left} мин"
        return True, reason
    return False, None


def record_action(
    adset_id: str, kind: str, prev_budget_usd: float, new_budget_usd: float,
    now: datetime | None = None,
) -> None:
    """Фиксирует реальное действие бота: время (last_raise_at/last_decrease_at),
    last_set_at и last_set_budget_usd (= new_budget_usd).

    Вызывать ТОЛЬКО после успешного set_adset_budget (active-режим).
    baseline при первом касании фиксируется из prev_budget_usd — только если он
    валиден (finite, положительный).

    Битый state → V2StateError (fail-closed, файл не перезаписываем пустым).
    """
    now_dt = _now_local(now)
    now_iso = now_dt.isoformat()
    with _v2_lock():
        state = _load_state_strict()  # битый → V2StateError (не затираем)
        entry = _get_adset(state, adset_id)
        if kind == "raise":
            entry["last_raise_at"] = now_iso
        else:
            entry["last_decrease_at"] = now_iso
        entry["last_set_at"] = now_iso
        if _is_valid_budget(new_budget_usd):
            entry["last_set_budget_usd"] = float(new_budget_usd)
        else:
            # Невалидный бюджет не должен служить опорой детекта ручной правки
            logger.warning(
                "record_action: невалидный new_budget_usd=%s adset=%s — last_set_budget обнулён",
                new_budget_usd, adset_id,
            )
            entry["last_set_budget_usd"] = None
        # baseline фиксируем только из валидного бюджета ДО действия
        if entry.get("baseline_budget_usd") is None and _is_valid_budget(prev_budget_usd):
            entry["baseline_budget_usd"] = float(prev_budget_usd)
        # prune реально вызывается из write-пути (ограничивает рост файла)
        _prune_adsets(state["adsets"], state["undo"], now_dt)
        _save_state(state)


# ---------------------------------------------------------------------------
# Baseline-бюджет (пол снижения считается от него)
# ---------------------------------------------------------------------------

def get_baseline_budget(
    adset_id: str, current_budget_usd: float, now: datetime | None = None
) -> float:
    """Опорный бюджет адсета. Первое касание → baseline = current_budget_usd
    (фиксируем и сохраняем), но ТОЛЬКО если current валиден (finite, положительный).
    Далее — возвращаем ранее зафиксированный валидный baseline.

    Битый state → V2StateError; нет валидного baseline и current невалиден →
    V2StateError (fail-closed: не сеем мусорный опорный бюджет).
    """
    with _v2_lock():
        state = _load_state_strict()
        entry = _get_adset(state, adset_id)
        stored = entry.get("baseline_budget_usd")
        if _is_valid_budget(stored):
            return float(stored)
        if not _is_valid_budget(current_budget_usd):
            raise V2StateError(
                f"нет валидного baseline и current_budget невалиден: {current_budget_usd!r}"
            )
        entry["baseline_budget_usd"] = float(current_budget_usd)
        _save_state(state)
        return float(current_budget_usd)


# ---------------------------------------------------------------------------
# Детект ручной правки владельца
# ---------------------------------------------------------------------------

def detect_manual_edit(
    adset_id: str, current_fb_budget_usd: float, tolerance_pct: float = 5.0,
    now: datetime | None = None,
) -> bool:
    """True если владелец, похоже, правил бюджет руками.

    Критерий: последний бот-выставленный бюджет свежий (last_set_at моложе 48ч)
    И текущий FB-бюджет расходится с ним больше, чем на tolerance_pct.
    При детекте — ребейзлайн: baseline и last_set_budget привязываем к текущему
    значению (считаем новый ручной бюджет опорным, отметку свежести обновляем),
    чтобы следующий прогон не сработал повторно.

    Невалидный текущий бюджет или битый state → False без записи (ребейзлайна нет,
    файл не затираем). Блокировка решения при битом state — на гейтах кулдауна/hold.
    """
    if not _is_valid_budget(current_fb_budget_usd):
        return False
    now_dt = _now_local(now)
    with _v2_lock():
        try:
            state = _load_state_strict()
        except V2StateError as exc:
            logger.error(
                "detect_manual_edit: v2-state битой — без ребейзлайна adset=%s: %s",
                adset_id, exc,
            )
            return False
        entry = state["adsets"].get(str(adset_id))
        if not isinstance(entry, dict):
            return False
        last_set_at = _parse_iso(entry.get("last_set_at"))
        last_set_budget = entry.get("last_set_budget_usd")
        if last_set_at is None or not _is_valid_budget(last_set_budget):
            return False
        age_hours = (now_dt - last_set_at).total_seconds() / 3600.0
        if age_hours > _MANUAL_EDIT_FRESH_HOURS:
            return False
        last_set_budget = float(last_set_budget)
        delta_pct = abs(current_fb_budget_usd - last_set_budget) / last_set_budget * 100.0
        if delta_pct <= tolerance_pct:
            return False
        # Ручная правка — ребейзлайн на текущее значение
        entry["baseline_budget_usd"] = float(current_fb_budget_usd)
        entry["last_set_budget_usd"] = float(current_fb_budget_usd)
        entry["last_set_at"] = now_dt.isoformat()
        _save_state(state)
    logger.info(
        "detect_manual_edit: адсет %s правлен вручную (было $%.2f, стало $%.2f, +%.1f%%) — ребейзлайн",
        adset_id, last_set_budget, current_fb_budget_usd, delta_pct,
    )
    return True


# ---------------------------------------------------------------------------
# Hold (заморозка адсета после ручного возврата бюджета)
# ---------------------------------------------------------------------------

def set_hold(adset_id: str, days: int, now: datetime | None = None) -> None:
    """Замораживает адсет на N дней (бот его не трогает до hold_until).

    Битый state → V2StateError (fail-closed, не затираем)."""
    now_dt = _now_local(now)
    until = now_dt + timedelta(days=int(days))
    with _v2_lock():
        state = _load_state_strict()
        entry = _get_adset(state, adset_id)
        entry["hold_until"] = until.isoformat()
        _save_state(state)


def is_on_hold(adset_id: str, now: datetime | None = None) -> bool:
    """True если адсет под hold прямо сейчас (now < hold_until).

    Битый state → True (fail-closed: считаем адсет замороженным, бот не трогает)."""
    try:
        with _v2_lock():
            state = _load_state_strict()
    except V2StateError as exc:
        logger.error(
            "is_on_hold: v2-state битой — fail-closed (считаем hold) adset=%s: %s",
            adset_id, exc,
        )
        return True
    entry = state["adsets"].get(str(adset_id))
    if not isinstance(entry, dict):
        return False
    until = _parse_iso(entry.get("hold_until"))
    if until is None:
        return False
    return _now_local(now) < until


# ---------------------------------------------------------------------------
# Undo-карта возврата бюджета (кнопка «↩️ Вернуть бюджет»)
# ---------------------------------------------------------------------------

def record_undo_entry(
    adset_id: str, prev_budget_usd: float, new_budget_usd: float,
    now: datetime | None = None,
) -> None:
    """Кладёт запись «можно вернуть prev_budget_usd» для кнопки undo.

    prev_budget_usd — бюджет ДО снижения (что вернуть); new_budget_usd — что
    выставлено при снижении. Ретеншн/обрезка — в _prune_undo (зеркало UNDO_MAP).
    Битый state → V2StateError (fail-closed, не затираем)."""
    now_dt = _now_local(now)
    with _v2_lock():
        state = _load_state_strict()
        state["undo"][str(adset_id)] = {
            "adset_id": str(adset_id),
            "prev_budget_usd": float(prev_budget_usd),
            "new_budget_usd": float(new_budget_usd),
            "at": now_dt.isoformat(),
            "done": False,
        }
        _prune_undo(state["undo"], now_dt)
        _save_state(state)


def get_undo_entry(adset_id: str) -> dict | None:
    """Возвращает undo-запись адсета или None (нет записи/битый state)."""
    try:
        with _v2_lock():
            state = _load_state_strict()
    except V2StateError as exc:
        logger.error("get_undo_entry: v2-state битой — None adset=%s: %s", adset_id, exc)
        return None
    entry = state["undo"].get(str(adset_id))
    return entry if isinstance(entry, dict) else None


def mark_undo_done(adset_id: str, now: datetime | None = None) -> bool:
    """Помечает undo-запись адсета выполненной. Идемпотентно.

    Returns:
        True если запись найдена и (была/стала) done; False если записи нет.
    Битый state → V2StateError (fail-closed, не затираем)."""
    with _v2_lock():
        state = _load_state_strict()
        entry = state["undo"].get(str(adset_id))
        if not isinstance(entry, dict):
            return False
        entry["done"] = True
        _save_state(state)
    return True


# ---------------------------------------------------------------------------
# Prune (ретеншн) — lock-free helper'ы + публичная точка входа
# ---------------------------------------------------------------------------

def _active_undo_ids(undo: dict) -> set:
    """adset_id незакрытых (done=False) undo-записей — их adset-состояние трогать нельзя."""
    out: set = set()
    for key, entry in undo.items():
        if isinstance(entry, dict) and not entry.get("done", False):
            out.add(str(key))
    return out


def _adset_newest_stamp(entry: dict) -> datetime | None:
    """Самая свежая из отметок действий адсета (для ретеншна/сортировки)."""
    stamps = [
        _parse_iso(entry.get("last_raise_at")),
        _parse_iso(entry.get("last_decrease_at")),
        _parse_iso(entry.get("last_set_at")),
    ]
    return max([s for s in stamps if s is not None], default=None)


def _adset_protected(entry: dict, key: str, now_dt: datetime, active_undo: set) -> bool:
    """Запись адсета нельзя удалять преждевременно, если:
    активный hold, незакрытый undo, или свежая ручная правка (last_set_at < 48ч)."""
    if not isinstance(entry, dict):
        return False
    hold_until = _parse_iso(entry.get("hold_until"))
    if hold_until is not None and hold_until >= now_dt:
        return True
    if key in active_undo:
        return True
    last_set = _parse_iso(entry.get("last_set_at"))
    if last_set is not None:
        age_hours = (now_dt - last_set).total_seconds() / 3600.0
        if age_hours <= _MANUAL_EDIT_FRESH_HOURS:
            return True
    return False


def _prune_undo(undo: dict, now: datetime) -> None:
    """In-place чистка undo: удаляет записи старше UNDO_RETENTION_DAYS (по 'at'),
    затем обрезает до MAX_UNDO_ENTRIES самых свежих. Зеркало
    services.autopilot._prune_undo_map."""
    cutoff = now - timedelta(days=UNDO_RETENTION_DAYS)
    for key in list(undo.keys()):
        at_dt = _parse_iso(undo.get(key, {}).get("at") if isinstance(undo.get(key), dict) else None)
        if at_dt is not None and at_dt < cutoff:
            del undo[key]

    if len(undo) <= MAX_UNDO_ENTRIES:
        return

    def _sort_key(item):
        _, entry = item
        at_dt = _parse_iso(entry.get("at") if isinstance(entry, dict) else None)
        return at_dt or datetime.min.replace(tzinfo=_TZ_LOCAL)

    freshest = {
        key for key, _ in sorted(undo.items(), key=_sort_key, reverse=True)[:MAX_UNDO_ENTRIES]
    }
    for key in list(undo.keys()):
        if key not in freshest:
            del undo[key]


def _prune_adsets(adsets: dict, undo: dict, now_dt: datetime) -> bool:
    """In-place чистка adset-записей по возрасту И размеру.

    Защищены (никогда не удаляются преждевременно): активный hold, незакрытый
    undo, свежая ручная правка. По возрасту удаляем только записи, у которых ЕСТЬ
    отметка действия и она старше ADSET_RETENTION_DAYS (записи без отметок —
    напр. только-baseline — не считаем протухшими, их держит потолок размера).
    Возвращает True если что-то удалено."""
    active_undo = _active_undo_ids(undo)
    cutoff = now_dt - timedelta(days=ADSET_RETENTION_DAYS)
    changed = False

    # 1. Битые записи + протухшие по возрасту (не защищённые)
    for key in list(adsets.keys()):
        entry = adsets.get(key)
        if not isinstance(entry, dict):
            del adsets[key]
            changed = True
            continue
        if _adset_protected(entry, key, now_dt, active_undo):
            continue
        newest = _adset_newest_stamp(entry)
        if newest is not None and newest < cutoff:
            del adsets[key]
            changed = True

    # 2. Потолок размера: защищённые держим всегда, среди остальных оставляем
    #    самые свежие, старые вытесняем.
    if len(adsets) > MAX_ADSET_ENTRIES:
        protected = {
            key for key, entry in adsets.items()
            if _adset_protected(entry, key, now_dt, active_undo)
        }
        nonprotected = [key for key in adsets if key not in protected]
        nonprotected.sort(
            key=lambda k: _adset_newest_stamp(adsets[k]) or datetime.min.replace(tzinfo=_TZ_LOCAL),
            reverse=True,
        )
        slots = max(0, MAX_ADSET_ENTRIES - len(protected))
        keep = set(nonprotected[:slots])
        for key in list(adsets.keys()):
            if key in protected or key in keep:
                continue
            del adsets[key]
            changed = True

    return changed


def prune(now: datetime | None = None) -> None:
    """Чистит state: undo по ретеншну (30дн/200) + неактивные adset-записи
    (возраст ADSET_RETENTION_DAYS + потолок MAX_ADSET_ENTRIES, с защитой активного
    hold/undo/свежей ручной правки). Сохраняет только если что-то изменилось.

    Битый state → пропуск без записи (fail-closed, не перезаписываем пустым)."""
    now_dt = _now_local(now)
    with _v2_lock():
        try:
            state = _load_state_strict()
        except V2StateError as exc:
            logger.error("prune: v2-state битой — пропуск (fail-closed): %s", exc)
            return

        before_undo = len(state["undo"])
        _prune_undo(state["undo"], now_dt)
        changed_adsets = _prune_adsets(state["adsets"], state["undo"], now_dt)

        if changed_adsets or len(state["undo"]) != before_undo:
            _save_state(state)
