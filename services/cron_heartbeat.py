"""
Сторож кронов (heartbeat) — только чтение/отметки, без мутаций FB/AMO.

Каждый крон при успешном (без исключения) прогоне через декоратор @heartbeat(...)
пишет отметку в единый атомарный data/cron_heartbeats.json. Сторож (run_cron_watchdog,
вызывается отдельным кроном раз в 30 мин) сверяет отметки с ожидаемым циклом × K и
шлёт Telegram-алерт (health-канал, дедуп 6ч) о молчащих кронах.

Декоратор НЕ ловит исключения — при ошибке отметка не пишется, исключение
пробрасывается дальше (вызывающий крон глушит его как сейчас).
"""

import functools
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Часовой пояс CityA UTC+5 (единый со всеми остальными модулями проекта)
_TZ_LOCAL = timezone(timedelta(hours=5))

# Файл с отметками кронов (единый для всего процесса)
_HB_FILE = Path(__file__).resolve().parent.parent / "data" / "cron_heartbeats.json"

# Файл дедупликации алертов сторожа (паттерн ads_watchdog_state.json)
_WATCHDOG_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "cron_watchdog_state.json"

# Лок на запись/чтение cron_heartbeats.json — job'ы могут уходить в to_thread,
# поэтому конкурентная запись возможна даже в рамках одного event-loop процесса
_HB_LOCK = threading.Lock()

# Множитель порога протухания: протух = age_minutes > expected_minutes * _STALE_MULT
_STALE_MULT = 3.0

# Не повторять алерт сторожа по одному крону чаще чем раз в N часов
_WATCHDOG_DEDUP_HOURS = 6


def _load_heartbeats_unlocked() -> dict:
    """Читает cron_heartbeats.json без лока (вызывать только под _HB_LOCK)."""
    if not _HB_FILE.exists():
        return {"heartbeats": {}}
    try:
        data = json.loads(_HB_FILE.read_text(encoding="utf-8"))
        if "heartbeats" not in data or not isinstance(data["heartbeats"], dict):
            data["heartbeats"] = {}
        return data
    except Exception as e:
        logger.warning("cron_heartbeat: не удалось прочитать %s: %s", _HB_FILE, e)
        return {"heartbeats": {}}


def _save_heartbeats_unlocked(data: dict) -> None:
    """Атомарно сохраняет cron_heartbeats.json (tmp+rename), без лока."""
    _HB_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _HB_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_HB_FILE)


def load_heartbeats() -> dict:
    """Публичное чтение отметок кронов.

    Returns:
        {"heartbeats": {name: {"at": iso, "expected_minutes": float, "critical": bool}}}
        Битый/отсутствующий файл -> {"heartbeats": {}} (fail-quiet).
    """
    with _HB_LOCK:
        return _load_heartbeats_unlocked()


def mark_ok(name: str, expected_minutes: float, critical: bool,
            now: datetime | None = None) -> None:
    """Отмечает успешный прогон крона `name`.

    Read-modify-write под локом + атомарная запись (tmp+rename). Ошибки записи
    логируются и НЕ поднимаются — heartbeat не должен ронять крон, который он сторожит.
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL)

    with _HB_LOCK:
        try:
            data = _load_heartbeats_unlocked()
            data["heartbeats"][name] = {
                "at": now.isoformat(),
                "expected_minutes": expected_minutes,
                "critical": critical,
            }
            _save_heartbeats_unlocked(data)
        except Exception as e:
            logger.error("cron_heartbeat: не удалось записать отметку %s: %s", name, e)


def heartbeat(name: str, expected_minutes: float,
              critical: bool = False) -> Callable:
    """Декоратор: при успешном (без исключения) вызове обёрнутой функции -> mark_ok(...).

    При исключении функции — отметку НЕ пишет, исключение пробрасывается дальше
    без изменений (вызывающий крон уже сам ловит и логирует ошибки).

    ОГРАНИЧЕНИЕ (OK ≠ бизнес-успех): mark_ok пишется при ЛЮБОМ не-бросившем
    возврате — включая ранний return по гейту и случай, когда крон сам поймал
    и проглотил внутреннюю ошибку (except -> logger.error без re-raise). Поэтому
    пульс может оставаться зелёным при устойчивом сбое (протухший FB CAPI/GA4-токен,
    ошибочный ответ API, который крон не бросает), и сторож свежести его не увидит.
    Для отлова таких «тихих» сбоев критичные кроны дополнительно зовут
    report_cron_failure/report_cron_success (см. ниже) — страж подряд-провалов шлёт
    критический алерт на N-м провале подряд НЕЗАВИСИМО от heartbeat-пульса.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            mark_ok(name, expected_minutes, critical)
            return result

        return wrapper

    return decorator


def check_stale(now: datetime | None = None) -> list[dict]:
    """Возвращает список протухших кронов.

    Протух = отметка ЕСТЬ и age_minutes > expected_minutes * _STALE_MULT.
    Крон без отметки вообще (нет ключа) НЕ считается протухшим — он мог не успеть
    отработать ни разу с деплоя; иначе сразу после старта был бы шквал ложных алертов.

    Returns:
        [{"name": str, "age_minutes": float, "expected_minutes": float,
          "critical": bool, "at": iso}]
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL)

    data = load_heartbeats()
    stale: list[dict] = []

    for name, entry in data.get("heartbeats", {}).items():
        at_str = entry.get("at")
        expected_minutes = entry.get("expected_minutes", 0)
        critical = entry.get("critical", False)
        if not at_str:
            continue
        try:
            at = datetime.fromisoformat(at_str)
            if at.tzinfo is None:
                at = at.replace(tzinfo=_TZ_LOCAL)
        except Exception:
            logger.warning("cron_heartbeat: битая дата отметки %s: %s", name, at_str)
            continue

        age_minutes = (now - at).total_seconds() / 60.0
        threshold = expected_minutes * _STALE_MULT
        if age_minutes > threshold:
            stale.append({
                "name": name,
                "age_minutes": age_minutes,
                "expected_minutes": expected_minutes,
                "critical": critical,
                "at": at_str,
            })

    return stale


def _load_watchdog_state() -> dict:
    """Загружает state-файл дедупликации алертов сторожа."""
    if not _WATCHDOG_STATE_FILE.exists():
        return {"alerts": {}}
    try:
        data = json.loads(_WATCHDOG_STATE_FILE.read_text(encoding="utf-8"))
        if "alerts" not in data:
            data["alerts"] = {}
        return data
    except Exception as e:
        logger.warning("cron_heartbeat: не удалось прочитать watchdog state: %s", e)
        return {"alerts": {}}


def _save_watchdog_state(state: dict) -> None:
    """Атомарно сохраняет state-файл дедупликации алертов сторожа."""
    _WATCHDOG_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _WATCHDOG_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_WATCHDOG_STATE_FILE)


def _is_deduped(alert_key: str, state: dict, now: datetime) -> bool:
    """True если алерт по этому крону уже отправлен в течение _WATCHDOG_DEDUP_HOURS часов."""
    last_sent_str = state.get("alerts", {}).get(alert_key)
    if not last_sent_str:
        return False
    try:
        last_sent = datetime.fromisoformat(last_sent_str)
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=_TZ_LOCAL)
        return (now - last_sent).total_seconds() < _WATCHDOG_DEDUP_HOURS * 3600
    except Exception:
        return False


def run_cron_watchdog(now: datetime | None = None) -> dict:
    """Сверяет отметки кронов, шлёт Telegram-алерт (health) по протухшим, дедуп 6ч.

    Read-only относительно FB/AMO — только чтение state-файлов и отправка в Telegram.

    Returns:
        {"stale": int, "alerts_sent": int, "alerts_skipped": int}
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL)

    stale_crons = check_stale(now)
    state = _load_watchdog_state()
    state_changed = False

    alerts_sent = 0
    alerts_skipped = 0

    for cron in stale_crons:
        alert_key = cron["name"]

        if _is_deduped(alert_key, state, now):
            alerts_skipped += 1
            logger.debug("cron_heartbeat: алерт дедуплицирован для %s", alert_key)
            continue

        severity = "🔴 КРИТИЧНЫЙ" if cron["critical"] else "🟡"
        text = (
            f"⚠️ <b>Сторож кронов</b>\n"
            f"{severity} крон <code>{cron['name']}</code> молчит "
            f"{cron['age_minutes']:.0f} мин (ожидалось ≤{cron['expected_minutes'] * _STALE_MULT:.0f} мин)"
        )

        try:
            from services.notifications import send_telegram
            sent = send_telegram(text, channel="health")
            if sent:
                state.setdefault("alerts", {})[alert_key] = now.isoformat()
                state_changed = True
                alerts_sent += 1
                logger.info("cron_heartbeat: алерт отправлен по крону %s", cron["name"])
            else:
                # Telegram не отправил -> не помечаем, повторим на следующем прогоне
                alerts_skipped += 1
                logger.warning("cron_heartbeat: Telegram не отправил алерт по %s", cron["name"])
        except Exception as e:
            alerts_skipped += 1
            logger.warning("cron_heartbeat: ошибка отправки алерта по %s: %s", cron["name"], e)

    if state_changed:
        try:
            _save_watchdog_state(state)
        except Exception as e:
            logger.error("cron_heartbeat: не удалось сохранить watchdog state: %s", e)

    return {
        "stale": len(stale_crons),
        "alerts_sent": alerts_sent,
        "alerts_skipped": alerts_skipped,
    }


# ---------------------------------------------------------------------------
# Страж повторных провалов (report_cron_failure / report_cron_success)
#
# Проблема, которую он закрывает: критичные кроны глушат исключения log-only
# (except -> logger.error без алерта), а @heartbeat ставит OK при любом
# не-бросившем возврате. Значит устойчивый сбой (протухший токен, ошибочный
# ответ API) невидим — пульс зелёный, а MQL/офлайн-конверсии не уходят неделями.
#
# Решение: критичные кроны в своём except зовут report_cron_failure(...), а в
# конце успешного пути — report_cron_success(...). Счётчик подряд-провалов лежит
# в отдельном атомарном state-файле; на _FAILURE_ALERT_THRESHOLD-м провале подряд
# шлём критический алерт (антиспам — не чаще раза в _FAILURE_ALERT_COOLDOWN_HOURS ч).
# При успехе счётчик сбрасывается и, если до этого алертили, шлём однократное «ожил».
# Всё fail-safe: битый state НЕ роняет крон и НЕ шлёт ложный алерт (лог warning).
# ---------------------------------------------------------------------------

# Файл счётчиков подряд-провалов (отдельный от heartbeats/watchdog-state)
_FAILURE_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "cron_failure_state.json"

# Лок на чтение/запись cron_failure_state.json (независим от _HB_LOCK)
_FAILURE_LOCK = threading.Lock()

# На каком по счёту провале ПОДРЯД слать критический алерт
_FAILURE_ALERT_THRESHOLD = 3

# Антиспам: повторный алерт по одному крону не чаще раза в N часов
_FAILURE_ALERT_COOLDOWN_HOURS = 6


def _load_failure_state_unlocked() -> tuple[dict, bool]:
    """Читает cron_failure_state.json. Возвращает (state, ok).

    ok=False только если файл ЕСТЬ, но битый (невалидный JSON / не тот формат) —
    тогда вызывающий не доверяет счётчику и НЕ алертит (fail-safe). Отсутствие
    файла — это ok=True с пустым state (это норма, а не битьё).
    """
    if not _FAILURE_STATE_FILE.exists():
        return {"crons": {}}, True
    try:
        data = json.loads(_FAILURE_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("crons"), dict):
            logger.warning("cron_failure: неверный формат %s — сбрасываю", _FAILURE_STATE_FILE)
            return {"crons": {}}, False
        return data, True
    except Exception as e:
        logger.warning("cron_failure: битый state-файл %s: %s", _FAILURE_STATE_FILE, e)
        return {"crons": {}}, False


def _save_failure_state_unlocked(state: dict) -> None:
    """Атомарно сохраняет cron_failure_state.json (tmp+rename), без лока."""
    _FAILURE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _FAILURE_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_FAILURE_STATE_FILE)


def _failure_alert_deduped(entry: dict, now: datetime) -> bool:
    """True если по крону уже слали алерт в течение _FAILURE_ALERT_COOLDOWN_HOURS часов."""
    last_alert_str = entry.get("last_alert_at")
    if not last_alert_str:
        return False
    try:
        last_alert = datetime.fromisoformat(last_alert_str)
        if last_alert.tzinfo is None:
            last_alert = last_alert.replace(tzinfo=_TZ_LOCAL)
        return (now - last_alert).total_seconds() < _FAILURE_ALERT_COOLDOWN_HOURS * 3600
    except Exception:
        # Битая дата — считаем что не дедуплено, но и не роняем (fail-safe)
        return False


def _send_failure_alert(cron_name: str, count: int, error) -> None:
    """Шлёт критический алерт о серии подряд-провалов крона (health-канал). Не бросает."""
    try:
        from services.notifications import send_critical_alert
        send_critical_alert(
            f"Крон {cron_name}: {count}-й провал подряд",
            str(error)[:500],
            channel="health",
        )
        logger.warning("cron_failure: алерт отправлен по крону %s (%d подряд)", cron_name, count)
    except Exception as e:
        logger.warning("cron_failure: не удалось отправить алерт по %s: %s", cron_name, e)


def _send_recovery_alert(cron_name: str) -> None:
    """Шлёт однократное «крон ожил» после серии провалов (health-канал). Не бросает."""
    try:
        from services.notifications import send_critical_alert
        send_critical_alert(
            f"✅ Крон {cron_name} ожил",
            "После серии подряд-провалов крон снова отработал без ошибки.",
            channel="health",
        )
        logger.info("cron_failure: крон %s восстановился после серии провалов", cron_name)
    except Exception as e:
        logger.warning("cron_failure: не удалось отправить «ожил» по %s: %s", cron_name, e)


def report_cron_failure(cron_name: str, error, now: datetime | None = None) -> None:
    """Учитывает ОДИН провал крона `cron_name`; на _FAILURE_ALERT_THRESHOLD-м провале
    ПОДРЯД шлёт критический алерт (антиспам _FAILURE_ALERT_COOLDOWN_HOURS ч).

    Вызывать внутри except-блока крона (после logger.error). НИКОГДА не бросает и не
    роняет крон: битый state -> лог warning, сброс, без алерта (fail-safe — счётчику
    нельзя доверять, поэтому не тревожим впустую).

    Args:
        cron_name: имя крона (совпадает с именем в @heartbeat).
        error: исключение/строка ошибки — попадёт в текст алерта (обрезается до 500).
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL)
    try:
        should_alert = False
        count = 0
        with _FAILURE_LOCK:
            state, ok = _load_failure_state_unlocked()
            if not ok:
                # Битому счётчику доверять нельзя — чинимся сбросом, но НЕ алертим
                _save_failure_state_unlocked({"crons": {}})
                return

            entry = state["crons"].setdefault(
                cron_name,
                {"consecutive_failures": 0, "alerted": False, "last_alert_at": None, "last_error": None},
            )
            entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
            entry["last_error"] = str(error)[:500]
            count = entry["consecutive_failures"]

            # Алертим на N-м подряд И если антиспам-окно уже прошло
            if count >= _FAILURE_ALERT_THRESHOLD and not _failure_alert_deduped(entry, now):
                should_alert = True
                entry["alerted"] = True
                entry["last_alert_at"] = now.isoformat()

            _save_failure_state_unlocked(state)

        # Сеть — вне лока
        if should_alert:
            _send_failure_alert(cron_name, count, error)
    except Exception as e:
        logger.warning("cron_failure: report_cron_failure упал (крон %s): %s", cron_name, e)


def report_cron_success(cron_name: str, now: datetime | None = None) -> None:
    """Сбрасывает счётчик подряд-провалов крона `cron_name`. Если до этого по крону
    уже слали алерт — однократно шлёт «✅ Крон X ожил».

    Вызывать в конце УСПЕШНОГО пути крона (после реальной работы, не после раннего
    return по гейту). НИКОГДА не бросает: битый state -> лог warning, сброс (fail-safe).
    На «чистом» кроне (не было провалов) не пишет файл — только читает.
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL)
    try:
        recovered = False
        with _FAILURE_LOCK:
            state, ok = _load_failure_state_unlocked()
            if not ok:
                _save_failure_state_unlocked({"crons": {}})
                return

            entry = state["crons"].get(cron_name)
            if not entry:
                return  # крон ни разу не падал — сбрасывать нечего, файл не трогаем

            was_alerted = bool(entry.get("alerted", False))
            had_failures = int(entry.get("consecutive_failures", 0)) > 0
            if not was_alerted and not had_failures:
                return  # уже чисто — лишнюю запись не делаем

            entry["consecutive_failures"] = 0
            entry["alerted"] = False
            entry["last_alert_at"] = None
            entry["last_error"] = None
            recovered = was_alerted  # «ожил» шлём только если ДО этого реально алертили
            _save_failure_state_unlocked(state)

        if recovered:
            _send_recovery_alert(cron_name)
    except Exception as e:
        logger.warning("cron_failure: report_cron_success упал (крон %s): %s", cron_name, e)
