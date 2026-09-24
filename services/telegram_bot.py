"""Legacy Telegram transport и read-only command router.

Callback-кнопки старого формата больше не являются контуром согласия и никогда
не запускают действие. Opaque ``oa:*`` обрабатывает только durable trusted inbox
из :mod:`services.owner_approval_telegram`.
"""

import html
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Путь к файлу состояния polling'а (offset)
_PROJECT_ROOT = Path(__file__).parent.parent
STATE_FILE = _PROJECT_ROOT / "data" / "telegram_state.json"

_LEGACY_CALLBACK_PREFIXES = (
    "undo:",
    "apply:",
    "applyrun:",
    "apfb:",
    "pause:",
    "stop_launch:",
    "undo_pause:",
    "oa:",
)

_STALE_BUTTON_TEXT = "Кнопка устарела — создай новое предложение"
_BRIEF_ID_RE = re.compile(r"^[A-Za-z0-9]{4,40}$")
_STOP_LAUNCH_ID_RE = re.compile(r"^[A-Za-z0-9]{4,40}$")
# Facebook ad_id в callback «↩️ Вернуть»: только цифры, 5-25 знаков.
_AD_ID_RE = re.compile(r"^[0-9]{5,25}$")


def _override_until_for_run(run) -> str:
    """Строит стабильный срок override от времени подтверждённой операции."""

    completed_at = next(
        (
            execution.completed_at
            for execution in reversed(getattr(run, "executions", ()))
            if execution.completed_at is not None
        ),
        None,
    )
    base = completed_at or datetime.now(ZoneInfo("Etc/GMT-5"))
    return (base.astimezone(ZoneInfo("Etc/GMT-5")) + timedelta(days=7)).isoformat()


def _deliver_action_notification(effect_id: str) -> str:
    """Доставляет старую outbox-запись без права инициировать действие."""

    try:
        from agent.database import claim_action_outbox, mark_action_outbox_sent

        claimed = claim_action_outbox(effect_id)
    except Exception as exc:
        logger.error(
            "Не удалось арендовать Telegram action outbox: %s",
            type(exc).__name__,
        )
        return "unavailable" if isinstance(exc, RuntimeError) else "failed"
    if claimed is None:
        return "unavailable"
    try:
        from services.notifications import send_telegram

        if claimed["channel"] != "TELEGRAM_ACTION":
            raise ValueError("Неожиданный канал action outbox")
        send_telegram(str(claimed["payload"]["text"]))
        mark_action_outbox_sent(effect_id)
        return "sent"
    except Exception as exc:
        logger.error(
            "Не удалось доставить Telegram action outbox: %s",
            type(exc).__name__,
        )
        return "failed"


def _load_offset() -> int:
    """Читает сохранённый offset из STATE_FILE. Возвращает 0 если файл не существует."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return int(data.get("offset", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return 0


def _save_offset(offset: int) -> None:
    """Атомарно сохраняет offset в STATE_FILE (через временный файл)."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Атомарная запись: пишем во временный файл, затем переименовываем
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=STATE_FILE.parent, prefix=".telegram_state_", suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump({"offset": offset}, f)
            os.replace(tmp_path, STATE_FILE)
        except Exception:
            # Убираем временный файл при ошибке
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("Не удалось сохранить offset в %s: %s", STATE_FILE, type(exc).__name__)


def send_with_buttons(text: str, buttons: list[list[tuple[str, str]]]) -> bool:
    """Отправляет сообщение в Telegram с inline-кнопками.

    Args:
        text: HTML-текст сообщения.
        buttons: матрица кнопок [[("Метка", "callback_data"), ...], ...].
                 Каждый внутренний список — один ряд кнопок.

    Returns:
        True при успехе, False при любой ошибке.
    """
    try:
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        import requests as _requests

        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.debug("Telegram не настроен — пропускаем send_with_buttons")
            return False

        # Строим inline_keyboard из матрицы кортежей
        inline_keyboard = [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in buttons
        ]

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": inline_keyboard},
        }

        resp = _requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
        result = resp.json()
        if result.get("ok"):
            return True
        logger.warning("Telegram ответил ошибкой на sendMessage: %s", result.get("description"))
        return False
    except Exception as exc:
        # Не логируем exc целиком — URL содержит токен бота
        logger.warning("Не удалось отправить сообщение с кнопками: %s", type(exc).__name__)
        return False


def poll_updates() -> None:
    """Единственный поллер бота: owner-ingress с курсором в БД.

    Вызывается планировщиком каждые 60 секунд. Если токен/chat_id не настроены —
    сразу возвращается.

    Второго getUpdates здесь больше НЕТ. Раньше сразу после trusted ingress этот
    же вызов ходил в Telegram со своим файловым офсетом
    (``data/telegram_state.json``) и забирал ``allowed_updates=["message"]``.
    Семантика getUpdates такова, что переданный offset подтверждает и удаляет
    апдейты с сервера для ВСЕХ клиентов бота, поэтому две ветки воровали
    апдейты друг у друга: команда пульта могла исчезнуть до owner-обработчика, а
    ``/digest`` — попасть в консоль, которая его не знала, и пропасть навсегда.

    Теперь апдейт ровно один раз попадает в durable ``telegram_update_inbox``, а
    ``process_trusted_telegram_inbox`` разбирает и решения по кнопкам, и команды
    пульта (``services.owner_approval_telegram._record_console_command``).
    Файловый офсет остаётся на диске, но больше не используется.
    """
    try:
        from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    except ImportError:
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        from services.approval_telegram import process_owner_decisions

        process_owner_decisions(worker_id="telegram-poll", limit=50)
    except Exception as exc:
        # Fail-closed: worker повторит и callback, и команду на следующем цикле —
        # апдейт уже сохранён в инбоксе, потерять его нельзя.
        logger.warning(
            "Trusted Telegram inbox недоступен: %s",
            type(exc).__name__,
        )


def _handle_callback(cb: dict) -> None:
    """Принимает только безопасный ack; старые action-кнопки протухли."""
    from config import TELEGRAM_CHAT_ID

    # --- Проверка безопасности ---
    from_id = str(cb.get("from", {}).get("id", ""))
    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
    owner_id = str(TELEGRAM_CHAT_ID)

    if from_id != owner_id or chat_id != owner_id:
        # Молча игнорируем чужие callback
        return

    callback_data = cb.get("data", "")
    callback_query_id = str(cb.get("id") or "")
    if not isinstance(callback_data, str) or not callback_query_id:
        return

    if callback_data == "ack":
        _answer_callback(callback_query_id, "Принято 👍")
        return

    if callback_data.startswith("approve_brief:"):
        brief_id = callback_data[len("approve_brief:"):]
        if _BRIEF_ID_RE.fullmatch(brief_id) is None:
            return
        _execute_approve_brief(brief_id, callback_query_id)
        return

    if callback_data.startswith("reject_brief:"):
        brief_id = callback_data[len("reject_brief:"):]
        if _BRIEF_ID_RE.fullmatch(brief_id) is None:
            return
        _execute_reject_brief(brief_id, callback_query_id)
        return

    if callback_data.startswith("undo_pause:"):
        # Кнопка «↩️ Вернуть» из отчёта автопилота: мутации не делает, но и
        # тупиком быть не должна — превращаем нажатие в UNPAUSE-предложение.
        ad_id = callback_data[len("undo_pause:"):]
        if _AD_ID_RE.fullmatch(ad_id) is None:
            # Битый payload старой кнопки: ничего не предлагаем и не эхоим его.
            _answer_callback(callback_query_id, _STALE_BUTTON_TEXT)
            return
        _execute_undo_pause(ad_id, callback_query_id)
        return

    if callback_data.startswith(_LEGACY_CALLBACK_PREFIXES):
        _answer_callback(callback_query_id, _STALE_BUTTON_TEXT)
        return

    logger.debug("Неизвестный callback_data '%s' — игнорируем", callback_data[:50])


def _execute_undo(ad_id: str, callback_query_id: str) -> None:
    """Legacy entry point оставлен только для безопасного ответа старой кнопке."""
    _answer_callback(callback_query_id, _STALE_BUTTON_TEXT)


def _execute_apply(ad_id: str, callback_query_id: str) -> None:
    """Legacy entry point оставлен только для безопасного ответа старой кнопке."""
    _answer_callback(callback_query_id, _STALE_BUTTON_TEXT)


def _execute_applyrun(run_key: str, callback_query_id: str) -> None:
    """Legacy entry point оставлен только для безопасного ответа старой кнопке."""
    _answer_callback(callback_query_id, _STALE_BUTTON_TEXT)


def _execute_apfb(verdict: str, ad_id: str, action: str, callback_query_id: str) -> None:
    """Legacy entry point: 👍/👎 из старых сообщений больше ничего не записывает.

    Оценка решения — не мутация Facebook, поэтому proposal для неё бессмыслен, а
    нажатие на кнопку из старого сообщения не несёт актуального контекста
    (объявление могло быть заменено). Кнопка убрана из генерации отчёта
    (services/autopilot.py::_send_live_telegram_report), здесь остаётся только
    честный ответ на клики по уже отправленным сообщениям.
    """
    _answer_callback(callback_query_id, _STALE_BUTTON_TEXT)


def _answer_callback(callback_query_id: str, text: str) -> None:
    """Подтверждает callback_query (показывает всплывающее уведомление в Telegram).

    Тихо проглатывает ошибки — answerCallbackQuery не критичен.
    """
    try:
        from config import TELEGRAM_BOT_TOKEN
        import requests as _requests

        if not TELEGRAM_BOT_TOKEN:
            return

        _requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=10,
        )
    except Exception as exc:
        logger.debug("answerCallbackQuery не удался: %s", type(exc).__name__)


def _execute_approve_brief(brief_id: str, callback_query_id: str) -> None:
    """Одобряет только Trello-ТЗ; Facebook action consent здесь отсутствует."""

    from services import pending_briefs
    from services.notifications import send_telegram

    brief = pending_briefs.get_brief(brief_id)
    if brief is None:
        _answer_callback(callback_query_id, "ТЗ не найдено — возможно, устарело")
        return
    status = brief.get("status")
    if status == pending_briefs.STATUS_APPROVED:
        _answer_callback(callback_query_id, "Уже одобрено ✅")
        return
    if status == pending_briefs.STATUS_REJECTED:
        _answer_callback(callback_query_id, "Это ТЗ уже отклонено ❌")
        return
    try:
        from integrations.trello import BRIEF_LIST_NAME, create_card, get_or_create_list

        list_id = get_or_create_list(BRIEF_LIST_NAME)
        card = create_card(
            list_id,
            brief["name"],
            brief["desc"],
            product=brief.get("product"),
        )
    except Exception as exc:
        logger.warning(
            "approve_brief: Trello упал для %s — %s",
            brief_id,
            type(exc).__name__,
        )
        _answer_callback(callback_query_id, "Не смог создать в Trello, нажми ещё раз")
        return

    card_url = card.get("url") or card.get("shortUrl") or ""
    pending_briefs.mark_approved(
        brief_id,
        card_id=card.get("id"),
        card_url=card_url,
    )
    _answer_callback(callback_query_id, "✅ В Trello")
    link = f"\n{card_url}" if card_url else ""
    send_telegram(
        f"✅ В Trello: <b>{html.escape(str(brief['name']))}</b> → ТЗ реклам{link}"
    )


def _execute_reject_brief(brief_id: str, callback_query_id: str) -> None:
    """Отклоняет только Trello-ТЗ; Facebook action consent здесь отсутствует."""

    from services import pending_briefs

    brief = pending_briefs.get_brief(brief_id)
    if brief is None:
        _answer_callback(callback_query_id, "ТЗ не найдено")
        return
    status = brief.get("status")
    if status == pending_briefs.STATUS_APPROVED:
        _answer_callback(callback_query_id, "Уже одобрено — карточка в Trello")
        return
    if status == pending_briefs.STATUS_REJECTED:
        _answer_callback(callback_query_id, "Уже отклонено")
        return
    pending_briefs.mark_rejected(brief_id)
    _answer_callback(callback_query_id, "❌ Отклонил, убрал из очереди")


def _execute_stop_launch(card_id: str, callback_query_id: str) -> None:
    """Legacy entry point оставлен только для безопасного ответа старой кнопке."""
    _answer_callback(callback_query_id, _STALE_BUTTON_TEXT)


def _execute_undo_pause(ad_id: str, callback_query_id: str) -> None:
    """Создаёт UNPAUSE-предложение владельцу вместо прямого возврата рекламы.

    Кнопка «↩️ Вернуть» из отчёта автопилота больше ничего не мутирует: FB
    трогает только execution boundary после одобрения. Здесь — единственный
    producer-вызов (``propose_unpause``), все fail-closed проверки (объявление
    существует, inventory полон, статус действительно PAUSED) делает сам
    gateway; его отказ отдаём владельцу честным текстом, а не «получилось».
    """

    try:
        from services.action_producer_gateway import (
            ProducerActionError,
            propose_unpause,
        )
        from services.approval_checker_models import ActionOrigin
    except Exception as exc:  # pragma: no cover - недоступный модуль producer
        logger.error("undo_pause: producer недоступен — %s", type(exc).__name__)
        _answer_callback(callback_query_id, "Не смог создать предложение, попробуй позже")
        return

    try:
        outcome = propose_unpause(
            ad_id,
            origin=ActionOrigin.TELEGRAM,
            scope=f"telegram-undo-pause:{ad_id}",
        )
    except ProducerActionError as exc:
        # Честная причина отказа gateway (например UNPAUSE_TARGET_NOT_PAUSED):
        # реклама уже активна или live-инвентарь неполон.
        logger.info("undo_pause %s: предложение не создано — %s", ad_id, exc)
        _answer_callback(callback_query_id, f"Не могу предложить возврат: {exc}")
        return
    except Exception as exc:
        logger.warning("undo_pause %s упал: %s", ad_id, type(exc).__name__)
        _answer_callback(callback_query_id, "Не смог создать предложение, попробуй позже")
        return

    if outcome.receipt is None:
        logger.warning("undo_pause %s: producer без квитанции (%s)", ad_id, outcome.action)
        _answer_callback(callback_query_id, "Не смог создать предложение, попробуй позже")
        return

    _answer_callback(
        callback_query_id,
        f"📨 Создано предложение №{outcome.receipt.proposal_id}, ждёт одобрения",
    )
    _deliver_owner_proposals()


def _deliver_owner_proposals() -> None:
    """Best-effort будильник доставки; durable DELIVERY_PENDING добьёт крон."""

    try:
        from services.approval_telegram import deliver_owner_proposals

        deliver_owner_proposals(worker_id="telegram-undo-pause", limit=20)
    except Exception as exc:
        logger.warning(
            "Предложение сохранено, но доставка отложена: %s",
            type(exc).__name__,
        )
