"""Дневной кап подъёмов бюджета per-adset (локальный календарный день).

Хранит на диск, сколько процентов бюджета уже подняли по каждому адсету
за ТЕКУЩИЙ локальный календарный день, и бюджет адсета на НАЧАЛО дня.
Несколько мелких подъёмов в день допустимы в сумме до лимита (15%).

State-файл: data/budget_daily_cap_state.json
Формат:
{
  "day": "2026-07-02",                 # текущий локальный календарный день (ISO)
  "adsets": {
      "<adset_id>": {
          "start_budget_usd": 100.0,   # бюджет на начало дня (первое обращение сегодня)
          "raised_pct": 7.5,           # суммарный ПОДТВЕРЖДЁННЫЙ (committed) % за сегодня
          "pending": {                 # НЕподтверждённые резервации (Wave 1B)
              "<operation_id>": {
                  "old_budget_usd": 100.0,
                  "new_budget_usd": 110.0,
                  "pct": 10.0,         # % от start_budget, который держит эта резервация
                  "ts": "2026-07-02T13:00:05+05:00"
              }
          }
      }
  }
}

Конкурентность (Wave 1B):
- Все read-modify-write JSON выполняются под меж-процессным flock (стабильный
  .lock-файл рядом со state, НЕ на самом JSON, который заменяется через rename)
  плюс внутри-процессный threading-lock.
- Запись атомарна: уникальный temp + fsync + os.replace.
- Денежный путь (reserve_raise) идёт по протоколу reservation → provider-call →
  commit/release: сначала на диск пишется pending (держит лимит), только потом
  дёргается FB, и лишь по подтверждённому результату pending переводится в
  committed (успех) либо снимается (явный отказ). Timeout/неясный результат
  оставляет pending — fail-closed, повтор запрещён до сверки (reconciliation).
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
except ImportError:  # pragma: no cover — прод только Linux
    fcntl = None

logger = logging.getLogger(__name__)

# Локальный часовой пояс (UTC+5 по умолчанию, настраивается здесь) — единый для всей группы модулей budget_*
_TZ_LOCAL = timezone(timedelta(hours=5))

# State-файл дневного капа (по образцу budget_scaler_state.json)
_CAP_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "budget_daily_cap_state.json"

# Допуск сравнения бюджетов при сверке (reconciliation), USD
_BUDGET_EPS_USD = 0.01
# Допуск на кап при резервировании (проценты) — от накопления float-погрешностей
_CAP_EPS_PCT = 1e-6

# Внутрипроцессная защита (потоки одного процесса). RLock — на случай, если
# публичная функция вдруг вызовет другую; flock при этом НЕ вкладываем (см. _cap_lock).
_CAP_TLOCK = threading.RLock()


class CapStateError(Exception):
    """Cap-state нечитаем/битой структуры — в active-режиме это fail-closed сигнал."""


def _cap_lock_path() -> Path:
    """Стабильный .lock-файл рядом со state. Блокируем именно его, а не JSON:
    JSON заменяется через rename, и flock на заменяемом файле потерялся бы."""
    return _CAP_STATE_FILE.with_name(_CAP_STATE_FILE.name + ".lock")


@contextmanager
def _cap_lock():
    """Меж-процессный (flock) + внутри-процессный (RLock) замок cap-state.

    ВАЖНО: не вкладывать один _cap_lock() в другой в том же потоке — flock на
    втором fd того же файла в том же процессе заблокирует сам себя. Публичные
    функции берут замок ровно один раз и не вызывают друг друга под замком.
    """
    with _CAP_TLOCK:
        if fcntl is None:  # pragma: no cover — платформа без fcntl
            yield
            return
        path = _cap_lock_path()
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


def _today_local(now: datetime | None = None) -> str:
    """ISO-строка локального календарного дня (YYYY-MM-DD). now=None → текущий момент."""
    moment = now if now is not None else datetime.now(_TZ_LOCAL)
    # Если передали naive datetime — считаем, что это уже локальное время
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_TZ_LOCAL)
    else:
        moment = moment.astimezone(_TZ_LOCAL)
    return moment.date().isoformat()


def _load_cap_state() -> dict:
    """Читает state (лениво). При ошибке/отсутствии → {'day': None, 'adsets': {}}.

    ВНИМАНИЕ: сам замок НЕ берёт — вызывающий обязан держать _cap_lock().
    Толерантен к битому файлу (для мониторинга/дефолта). Денежный путь использует
    строгий _load_cap_state_strict (fail-closed)."""
    default: dict = {"day": None, "adsets": {}}
    if not _CAP_STATE_FILE.exists():
        return default
    try:
        data = json.loads(_CAP_STATE_FILE.read_text(encoding="utf-8"))
        default.update(data)
        # Защита от битого формата adsets (не dict)
        if not isinstance(default.get("adsets"), dict):
            default["adsets"] = {}
        return default
    except Exception as exc:
        logger.warning("_load_cap_state: не удалось прочитать — %s", exc)
        return default


def _load_cap_state_strict() -> dict:
    """Строгое чтение для денежного пути: битый/нечитаемый файл → CapStateError.

    В active это означает fail-closed (НЕ пустой лимит). Замок НЕ берёт."""
    if not _CAP_STATE_FILE.exists():
        return {"day": None, "adsets": {}}
    try:
        raw = _CAP_STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        raise CapStateError(f"cap-state нечитаем: {exc}") from exc
    if not isinstance(data, dict):
        raise CapStateError("cap-state не dict")
    adsets = data.get("adsets", {})
    if not isinstance(adsets, dict):
        raise CapStateError("cap-state.adsets не dict")
    data.setdefault("day", None)
    data["adsets"] = adsets
    return data


def _save_cap_state(state: dict) -> None:
    """Атомарно сохраняет state: уникальный temp + fsync + os.replace.

    Замок НЕ берёт — вызывающий держит _cap_lock(). Ошибку пробрасывает
    (вызывающий денежного пути трактует её как fail-closed)."""
    _CAP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CAP_STATE_FILE.with_name(
        f"{_CAP_STATE_FILE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _CAP_STATE_FILE)
    except Exception as exc:
        logger.error("_save_cap_state: не удалось сохранить — %s", exc)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


def _rollover_if_new_day(state: dict, today: str) -> dict:
    """Если state['day'] != today → сбрасывает adsets на пустой и ставит day=today.
    Возвращает (изменённый) state. НЕ пишет на диск — вызывающий сам сохраняет."""
    if state.get("day") != today:
        state["day"] = today
        state["adsets"] = {}
    return state


def _pending_pct(entry: dict) -> float:
    """Суммарный % НЕподтверждённых (pending) резерваций адсета."""
    pending = entry.get("pending", {})
    if not isinstance(pending, dict):
        return 0.0
    total = 0.0
    for res in pending.values():
        try:
            total += float(res.get("pct", 0.0))
        except (TypeError, ValueError):
            continue
    return total


def _validate_positive_finite(*values: float) -> bool:
    """Все значения конечны и строго положительны."""
    for v in values:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(fv) or fv <= 0:
            return False
    return True


def get_day_start_budget(adset_id: str, current_budget_usd: float,
                         now: datetime | None = None) -> float:
    """Возвращает бюджет адсета на начало дня.

    Если адсет сегодня ещё не фигурировал — фиксирует current_budget_usd
    как start_budget_usd (и raised_pct=0), сохраняет state, возвращает его.
    Если фигурировал — возвращает ранее зафиксированный start_budget_usd.
    Делает rollover при смене дня перед чтением.
    """
    today = _today_local(now)
    with _cap_lock():
        state = _load_cap_state()
        state = _rollover_if_new_day(state, today)

        adset_key = str(adset_id)
        adsets = state["adsets"]
        if adset_key not in adsets:
            adsets[adset_key] = {"start_budget_usd": current_budget_usd, "raised_pct": 0.0}
            _save_cap_state(state)
            return current_budget_usd

        return float(adsets[adset_key].get("start_budget_usd", current_budget_usd))


def get_remaining_daily_pct(adset_id: str, current_budget_usd: float,
                            daily_cap_pct: float,
                            now: datetime | None = None) -> float:
    """Сколько ПРОЦЕНТОВ ещё можно поднять этому адсету СЕГОДНЯ.

    remaining = max(0.0, daily_cap_pct - committed_pct - pending_pct).
    Фиксирует start_budget при первом обращении за день.
    Возвращает 0.0 если лимит на сегодня исчерпан.
    """
    today = _today_local(now)
    with _cap_lock():
        state = _load_cap_state()
        state = _rollover_if_new_day(state, today)

        adset_key = str(adset_id)
        adsets = state["adsets"]
        entry = adsets.get(adset_key)
        if entry is None:
            # Первое обращение за день — фиксируем start_budget (как get_day_start_budget)
            entry = {"start_budget_usd": current_budget_usd, "raised_pct": 0.0}
            adsets[adset_key] = entry
            _save_cap_state(state)

        start_budget = float(entry.get("start_budget_usd", current_budget_usd))
        if start_budget <= 0:
            # Защита от деления на ноль (адсет без бюджета/CBO) — капа фактически нет
            return 0.0

        raised = float(entry.get("raised_pct", 0.0))
        pending = _pending_pct(entry)
        return max(0.0, daily_cap_pct - raised - pending)


def record_raise(adset_id: str, current_budget_usd: float,
                 new_budget_usd: float, now: datetime | None = None) -> None:
    """Учитывает реальный подъём: прибавляет к raised_pct адсета
    процент прироста ОТ БЮДЖЕТА НА НАЧАЛО ДНЯ.

    added_pct = (new_budget_usd - current_budget_usd) / start_budget_usd * 100
    raised_pct += added_pct. Сохраняет state.

    Оставлен для совместимости/прямого учёта. Денежный конкурентный путь идёт
    через apply_capped_raise (reservation → commit). Вызывать ТОЛЬКО после
    успешного set_adset_budget (active-режим).
    """
    today = _today_local(now)
    with _cap_lock():
        state = _load_cap_state()
        state = _rollover_if_new_day(state, today)

        adset_key = str(adset_id)
        adsets = state["adsets"]
        if adset_key not in adsets:
            # Обращение к record_raise без предварительного get_remaining_daily_pct —
            # фиксируем start_budget как current_budget_usd (до подъёма).
            adsets[adset_key] = {"start_budget_usd": current_budget_usd, "raised_pct": 0.0}

        entry = adsets[adset_key]
        start_budget = float(entry.get("start_budget_usd", current_budget_usd))
        if start_budget <= 0:
            logger.warning(
                "record_raise: start_budget_usd<=0 для adset_id=%s — пропускаем учёт прироста", adset_id
            )
            _save_cap_state(state)
            return

        added_pct = (new_budget_usd - current_budget_usd) / start_budget * 100.0
        entry["raised_pct"] = float(entry.get("raised_pct", 0.0)) + added_pct
        _save_cap_state(state)


# ---------------------------------------------------------------------------
# Транзакционный протокол резерваций (Wave 1B): reserve → provider → commit
# ---------------------------------------------------------------------------

def reserve_raise(adset_id: str, current_budget_usd: float, new_budget_usd: float,
                  daily_cap_pct: float, now: datetime | None = None) -> str | None:
    """Атомарно резервирует место под подъём в дневном капе (fail-closed).

    Под замком: строгое чтение state (битый → fail-closed None), rollover локального дня,
    фиксация start_budget, валидация бюджетов/процента, проверка cap с учётом
    committed+pending, запись УНИКАЛЬНОЙ pending-резервации на диск.

    Возвращает operation_id при успехе; None — если резервировать нельзя
    (кап исчерпан, невалидные значения, битый state, ошибка сохранения).
    После None вызывающий НЕ должен трогать FB.
    """
    today = _today_local(now)
    try:
        with _cap_lock():
            try:
                state = _load_cap_state_strict()
            except CapStateError as exc:
                logger.error(
                    "reserve_raise: cap-state битой — fail-closed, adset=%s: %s", adset_id, exc
                )
                return None

            state = _rollover_if_new_day(state, today)
            adset_key = str(adset_id)
            adsets = state["adsets"]
            entry = adsets.get(adset_key)
            if entry is None:
                entry = {"start_budget_usd": current_budget_usd, "raised_pct": 0.0}
                adsets[adset_key] = entry

            start_budget = float(entry.get("start_budget_usd", current_budget_usd))

            # Валидация: бюджеты конечны и положительны, new > current
            if not _validate_positive_finite(current_budget_usd, new_budget_usd, start_budget):
                logger.warning(
                    "reserve_raise: невалидные бюджеты adset=%s cur=%s new=%s start=%s",
                    adset_id, current_budget_usd, new_budget_usd, start_budget,
                )
                return None
            if not (math.isfinite(daily_cap_pct) and daily_cap_pct > 0):
                logger.warning("reserve_raise: невалидный daily_cap_pct=%s adset=%s", daily_cap_pct, adset_id)
                return None

            pct = (new_budget_usd - current_budget_usd) / start_budget * 100.0
            if not math.isfinite(pct) or pct <= 0:
                logger.info("reserve_raise: неположительный/невалидный pct=%s adset=%s — отказ", pct, adset_id)
                return None

            raised = float(entry.get("raised_pct", 0.0))
            pending_now = _pending_pct(entry)
            remaining = daily_cap_pct - raised - pending_now
            if pct > remaining + _CAP_EPS_PCT:
                logger.info(
                    "reserve_raise: adset=%s pct=%.3f > remaining=%.3f (cap=%.1f, committed=%.3f, pending=%.3f) — отказ",
                    adset_id, pct, remaining, daily_cap_pct, raised, pending_now,
                )
                return None

            op_id = uuid.uuid4().hex
            pending = entry.setdefault("pending", {})
            if not isinstance(pending, dict):
                pending = {}
                entry["pending"] = pending
            ts = (now if now is not None else datetime.now(_TZ_LOCAL))
            pending[op_id] = {
                "old_budget_usd": float(current_budget_usd),
                "new_budget_usd": float(new_budget_usd),
                "pct": float(pct),
                "ts": ts.isoformat(),
            }
            _save_cap_state(state)  # ошибка сохранения → except ниже → None (fail-closed)
            return op_id
    except Exception as exc:
        logger.error("reserve_raise: ошибка резервации adset=%s — fail-closed: %s", adset_id, exc)
        return None


def _resolve_reservation(adset_id: str, op_id: str, commit: bool) -> None:
    """Внутреннее: снять pending-резервацию (commit=True → перевести в raised_pct;
    commit=False → просто удалить). БЕЗ rollover — резолвим ровно ту операцию,
    что была зарезервирована (жизненный цикл — секунды, обычно тот же день).
    Идемпотентно: отсутствие op_id/adset → no-op."""
    with _cap_lock():
        state = _load_cap_state()
        entry = state.get("adsets", {}).get(str(adset_id))
        if not entry:
            return
        pending = entry.get("pending", {})
        if not isinstance(pending, dict) or op_id not in pending:
            return
        res = pending.pop(op_id)
        if commit:
            entry["raised_pct"] = float(entry.get("raised_pct", 0.0)) + float(res.get("pct", 0.0))
        if not pending:
            entry.pop("pending", None)
        _save_cap_state(state)


def commit_reservation(adset_id: str, op_id: str, now: datetime | None = None) -> None:
    """Подтверждает резервацию: pending → committed (raised_pct += pct)."""
    _resolve_reservation(adset_id, op_id, commit=True)


def release_reservation(adset_id: str, op_id: str, now: datetime | None = None) -> None:
    """Снимает резервацию без учёта в raised_pct (FB явно отказал, бюджет не менялся)."""
    _resolve_reservation(adset_id, op_id, commit=False)


def list_pending_adsets(now: datetime | None = None) -> list[str]:
    """adset_id, у которых есть незавершённые pending-резервации (для сверки)."""
    with _cap_lock():
        state = _load_cap_state()
        out: list[str] = []
        for aid, entry in state.get("adsets", {}).items():
            pending = entry.get("pending", {}) if isinstance(entry, dict) else {}
            if isinstance(pending, dict) and pending:
                out.append(aid)
        return out


def reconcile_pending(adset_id: str, actual_budget_usd: float | None,
                      now: datetime | None = None) -> dict:
    """Сверяет зависшие pending по ФАКТИЧЕСКОМУ бюджету адсета в FB.

    - actual == new_budget (в пределах eps) → FB применил → commit.
    - actual == old_budget (в пределах eps) → без изменений → release.
    - actual недоступен (None/не finite) или неоднозначен → оставить pending
      (fail-closed, повышать нельзя).

    Возвращает {'committed': n, 'released': m, 'kept': k}.
    """
    result = {"committed": 0, "released": 0, "kept": 0}
    with _cap_lock():
        state = _load_cap_state()
        entry = state.get("adsets", {}).get(str(adset_id))
        if not entry:
            return result
        pending = entry.get("pending", {})
        if not isinstance(pending, dict) or not pending:
            return result

        actual_ok = actual_budget_usd is not None and math.isfinite(float(actual_budget_usd))
        changed = False
        for op_id in list(pending.keys()):
            res = pending[op_id]
            try:
                new_b = float(res.get("new_budget_usd"))
                old_b = float(res.get("old_budget_usd"))
                pct = float(res.get("pct", 0.0))
            except (TypeError, ValueError):
                # битая запись резервации — оставляем (не угадываем), помечаем kept
                result["kept"] += 1
                continue

            if not actual_ok:
                result["kept"] += 1
                continue

            actual = float(actual_budget_usd)
            if abs(actual - new_b) <= _BUDGET_EPS_USD:
                # FB применил подъём (но мы упали до commit) → фиксируем
                entry["raised_pct"] = float(entry.get("raised_pct", 0.0)) + pct
                del pending[op_id]
                result["committed"] += 1
                changed = True
            elif abs(actual - old_b) <= _BUDGET_EPS_USD:
                # Бюджет не менялся → FB не применил → снимаем резервацию
                del pending[op_id]
                result["released"] += 1
                changed = True
            else:
                # Неоднозначно (бюджет иной) → оставляем pending
                result["kept"] += 1

        if not pending:
            entry.pop("pending", None)
        if changed:
            _save_cap_state(state)
    return result


def _normalize_outcome(raw: object) -> str:
    """Нормализует результат provider-мутации в 'success' | 'rejected' | 'unknown'.

    True → success (FB 200), False → rejected (FB явно не-200, бюджет не менялся),
    строки success/rejected/unknown пропускаются как есть, всё прочее → unknown."""
    if raw is True:
        return "success"
    if raw is False:
        return "rejected"
    if isinstance(raw, str) and raw in ("success", "rejected", "unknown"):
        return raw
    return "unknown"


def apply_capped_raise(adset_id: str, current_budget_usd: float, new_budget_usd: float,
                       daily_cap_pct: float, mutate_fn, now: datetime | None = None) -> dict:
    """Полный атомарный протокол подъёма под дневным капом.

    reserve → (вне замка) provider-call mutate_fn() → commit/release/keep.

    mutate_fn() возвращает bool (True=успех/False=отказ) ИЛИ строку
    'success'/'rejected'/'unknown'. Исключение из mutate_fn трактуется как
    'unknown' (timeout/неясно) — резервация ОСТАЁТСЯ pending (fail-closed).

    Возвращает {'status': ..., 'op_id': ...}, где status:
    - 'capped'    — резервировать нельзя (кап/валидация/битый state), FB не звали;
    - 'committed' — FB подтвердил, подъём учтён;
    - 'rejected'  — FB явно отказал, резервация снята;
    - 'pending'   — результат неизвестен, резервация удержана (повтор запрещён).
    """
    op_id = reserve_raise(adset_id, current_budget_usd, new_budget_usd, daily_cap_pct, now=now)
    if op_id is None:
        return {"status": "capped", "op_id": None}

    # Provider-call ВНЕ замка: pending уже на диске и держит лимит. Даже падение
    # процесса здесь оставит pending → кап защищён, сверка разрулит на след. прогоне.
    try:
        raw = mutate_fn()
    except Exception as exc:
        logger.warning(
            "apply_capped_raise: mutate_fn исключение adset=%s — pending удержан (fail-closed): %s",
            adset_id, exc,
        )
        return {"status": "pending", "op_id": op_id}

    outcome = _normalize_outcome(raw)
    if outcome == "success":
        try:
            commit_reservation(adset_id, op_id, now=now)
            return {"status": "committed", "op_id": op_id}
        except Exception as exc:
            # FB применил, но локальный commit не удался → pending остаётся на диске
            # (кап защищён от повторного подъёма), сверка подтвердит позже.
            logger.error(
                "apply_capped_raise: commit не удался после FB-успеха adset=%s — pending остаётся: %s",
                adset_id, exc,
            )
            return {"status": "pending", "op_id": op_id}
    if outcome == "rejected":
        try:
            release_reservation(adset_id, op_id, now=now)
        except Exception as exc:
            logger.warning("apply_capped_raise: release не удался adset=%s: %s", adset_id, exc)
        return {"status": "rejected", "op_id": op_id}

    # unknown → оставляем pending (fail-closed)
    return {"status": "pending", "op_id": op_id}
