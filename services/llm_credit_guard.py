"""
Страж кредитов Anthropic API.

Зачем: без стража бот может молча сжечь тысячи LLM-вызовов на ошибке «credit balance is too low» —
каждый вызов падал в fail-safe (пустой сценарий / продукт ОБЩАЯ) и лог-warning, а
батч-циклы разметки продолжают молотить впустую, а алерт владельцу не уходит.

Что делает страж:
1. is_credit_error(exc) — распознаёт ошибку исчерпания кредитов/биллинга Anthropic
   по тексту исключения (SDK бросает BadRequestError с телом «Your credit balance is
   too low ... Plan & Billing ...»).
2. alert_credit_exhausted(...) — шлёт критический алерт ОДИН раз (антиспам через
   state-файл, не чаще раза в час — паттерн дедупа как в services/cron_heartbeat.py).
3. raise_if_credit_error(...) — если это ошибка кредитов: алерт (дедуп) + бросает
   CreditBalanceError. Батч-циклы (backfill_target_product, generate_and_push_briefs,
   get_learning_v2) ловят CreditBalanceError и прерываются сразу — молотить смысла нет.

Всё fail-safe: сам страж НИКОГДА не роняет вызывающий крон (кроме намеренного
CreditBalanceError). Битый state-файл → лог warning, алерт всё равно уходит.
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Часовой пояс CityA UTC+5 (единый со всеми модулями проекта)
_TZ_LOCAL = timezone(timedelta(hours=5))

# State-файл дедупликации алерта об исчерпании кредитов (паттерн cron_watchdog_state.json)
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "llm_credit_alert_state.json"

# Лок на чтение/запись state-файла — LLM-вызовы уходят в to_thread / ThreadPoolExecutor,
# конкурентная запись возможна даже в рамках одного процесса
_LOCK = threading.Lock()

# Антиспам: повторный алерт не чаще раза в N часов
_ALERT_COOLDOWN_HOURS = 1

# Маркеры ошибки исчерпания кредитов / биллинга Anthropic (проверяем в нижнем регистре).
# Точечно, чтобы не ловить произвольные сетевые/валидационные ошибки как «кончились кредиты».
_CREDIT_MARKERS: tuple[str, ...] = (
    "credit balance is too low",
    "credit balance",
    "plan & billing",
    "plan and billing",
    "purchase credits",
    "insufficient credit",
    "billing_error",
)


class CreditBalanceError(RuntimeError):
    """Сигнал: у Anthropic API кончились кредиты (или проблема биллинга).

    Бросается raise_if_credit_error после отправки (дедуплицированного) алерта.
    Батч-циклы разметки должны поймать его и прерваться — продолжать бессмысленно,
    все следующие вызовы упадут так же.
    """


def is_credit_error(exc: object) -> bool:
    """True, если исключение похоже на исчерпание кредитов / биллинг Anthropic.

    Распознаём по тексту (SDK кладёт человекочитаемое сообщение в str(exc)).
    Регистр не важен. Не-строковые/пустые случаи → False (не наш сигнал).
    """
    text = str(exc).lower()
    if not text:
        return False
    return any(marker in text for marker in _CREDIT_MARKERS)


def _load_state_unlocked() -> dict:
    """Читает state-файл без лока (вызывать под _LOCK). Битый/нет файла → пустой state."""
    if not _STATE_FILE.exists():
        return {}
    try:
        data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("llm_credit_guard: не удалось прочитать state %s: %s", _STATE_FILE, e)
        return {}


def _save_state_unlocked(state: dict) -> None:
    """Атомарно сохраняет state (tmp + rename), без лока."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_STATE_FILE)


def _is_deduped(state: dict, now: datetime) -> bool:
    """True, если алерт уже отправляли в течение _ALERT_COOLDOWN_HOURS часов."""
    last_sent_str = state.get("last_alert_at")
    if not last_sent_str:
        return False
    try:
        last_sent = datetime.fromisoformat(last_sent_str)
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=_TZ_LOCAL)
        return (now - last_sent).total_seconds() < _ALERT_COOLDOWN_HOURS * 3600
    except Exception:
        # Битую метку игнорируем — считаем что не дедуплено (лучше лишний алерт, чем тишина)
        return False


def alert_credit_exhausted(context: str, error: object, now: datetime | None = None) -> bool:
    """Шлёт критический алерт «кончились кредиты Anthropic API» — ОДИН раз в час.

    Антиспам глобальный (один ключ last_alert_at на весь процесс): «кредиты кончились» —
    состояние аккаунта, а не конкретного вызова, поэтому дедупим независимо от context.

    Args:
        context: где поймали (имя функции) — попадёт в текст алерта.
        error: исключение/строка ошибки — обрезается до 300 символов в тексте.

    Returns:
        True, если алерт реально отправлен; False, если задедуплен/не удалось.
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL)

    try:
        with _LOCK:
            state = _load_state_unlocked()
            if _is_deduped(state, now):
                logger.warning(
                    "llm_credit_guard: кредиты Anthropic кончились (%s) — алерт задедуплен (был <1ч назад)",
                    context,
                )
                return False
            # Помечаем ДО отправки, чтобы параллельные вызовы под локом не задублировали.
            state["last_alert_at"] = now.isoformat()
            state["last_context"] = context
            _save_state_unlocked(state)
    except Exception as e:
        # Битый state не должен мешать отправить алерт — идём отправлять без дедупа
        logger.warning("llm_credit_guard: сбой state-файла (%s) — шлю алерт без дедупа", e)

    try:
        from services.notifications import send_critical_alert

        send_critical_alert(
            "🛑 Кончились кредиты Anthropic API",
            f"LLM-вызовы падают на 'credit balance is too low'. Разметка/сценаристы "
            f"остановлены до пополнения баланса.\nМесто: {context}\n{str(error)[:300]}",
            channel="ads",
        )
        logger.error("llm_credit_guard: отправлен алерт об исчерпании кредитов Anthropic (%s)", context)
        return True
    except Exception as e:
        logger.warning("llm_credit_guard: не удалось отправить алерт об исчерпании кредитов: %s", e)
        return False


def raise_if_credit_error(context: str, exc: object, now: datetime | None = None) -> None:
    """Если exc — ошибка кредитов Anthropic: шлёт дедуплицированный алерт и бросает
    CreditBalanceError (чтобы батч-цикл прервался). Иначе — ничего не делает.

    Вызывать ВНУТРИ except-блока LLM-вызова, ПЕРЕД обычным fail-safe возвратом:

        except Exception as exc:
            raise_if_credit_error("module._fn", exc)   # алерт + raise, если это кредиты
            logger.warning(...)
            return fallback

    Args:
        context: имя функции для текста алерта.
        exc: пойманное исключение.

    Raises:
        CreditBalanceError: если is_credit_error(exc) True.
    """
    if not is_credit_error(exc):
        return
    alert_credit_exhausted(context, exc, now=now)
    raise CreditBalanceError(str(exc)[:300]) from (exc if isinstance(exc, BaseException) else None)
