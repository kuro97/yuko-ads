"""Telegram-команды владельца: отчёты и immutable action proposals.

Команды ``/pause``, ``/unpause``, ``/scale`` и ``/launch`` никогда не выполняют
действие. Они сохраняют proposal, после чего отдельный trusted T3-контур
доставляет opaque ``oa:*`` кнопки и пишет решение владельца.

Безопасность: команды принимаются ТОЛЬКО от владельца
(from.id == chat.id == int(TELEGRAM_CHAT_ID)). Чужие апдейты и свободный
текст молча игнорируются (без ответа, без подтверждения существования бота).

Ни одна публичная функция не бросает исключений наружу — ошибки логируются
через type(exc).__name__, владельцу (не постороннему) уходит явное сообщение
о провале команды.
"""

import html
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Mapping

logger = logging.getLogger(__name__)

# Лимит Telegram на одно сообщение (символов)
_TELEGRAM_MAX_LEN = 4096

# Часовой пояс CityA (UTC+5) — единый со всеми остальными модулями проекта
_TZ_LOCAL = timezone(timedelta(hours=5))

# Whitelist команд пульта: используется и как список известных команд в
# _parse_command, и как текст описаний в _cmd_help.
_COMMANDS: dict[str, str] = {
    "status": "срез сейчас — деньги, что сделал бот, юнитка",
    "queue": "очередь запусков (топ-10 карточек «Готово»)",
    "ads": "топ-8 активных объявлений по расходу за 7д",
    "scale": "предложить масштабирование бюджетов",
    "launch": "предложить запуск карточки из очереди",
    "pause": "предложить паузу: /pause <ad_id>",
    "unpause": "предложить возврат: /unpause <ad_id>",
    "digest": "прислать пакет предложений прямо сейчас (кнопки решений)",
    "products": "сводка «какого продукта больше» (PRODA/PRODB/СТАРТ/ОБЩАЯ)",
    "brief": "сгенерировать ТЗ и прислать на одобрение",
    "hourly": "почасовой сбор младше 48ч; /hourly <ad_id> — ряд по часам",
    "help": "список команд пульта",
}

# Сколько карточек показываем в /queue и сколько из них помечаем «уйдут завтра»
_QUEUE_TOP = 10
_QUEUE_FIRST_BATCH = 5

# Сколько объявлений показываем в /ads
_ADS_TOP = 8

# Кэш последнего среза /ads: ad_id -> безопасные подписи для PAUSE/UNPAUSE
# proposal. Он не является доказательством и не разрешает исполнение.
_ADS_CACHE: dict[str, dict] = {}
_AD_ID_RE = re.compile(r"^[0-9]{5,25}$")
_PROPOSAL_TTL = timedelta(hours=4)


# ---------------------------------------------------------------------------
# Роутер (точка входа из telegram_bot.poll_updates)
# ---------------------------------------------------------------------------

def handle_message(msg: dict) -> None:
    """Обрабатывает одно СЫРОЕ telegram-сообщение (update['message']).

    Безопасность: если from.id != chat.id != TELEGRAM_CHAT_ID → молча игнор
    (debug-лог). Извлекает текст, нормализует команду (нижний регистр,
    отсекает @botname и аргументы), диспатчит в _dispatch. Не команда /
    неизвестная команда → игнор без ответа. Никогда не бросает исключений.

    Боевой путь команд теперь другой: единственный поллер бота — owner-ingress
    с курсором в БД, и он зовёт ``dispatch_owner_text`` уже после собственной
    проверки владельца (см. ``owner_approval_telegram._record_console_command``).
    Эта функция остаётся входом для сырого апдейта со своей проверкой владельца.
    """
    try:
        if not _is_owner(msg):
            logger.debug("telegram_console: сообщение не от владельца — игнор")
            return

        text = msg.get("text")
        command = _parse_command(text)
        if command is None:
            logger.debug("telegram_console: не команда / неизвестная команда — игнор")
            return

        args = _parse_args(text)
        source_ref = (
            f"telegram-message:{msg.get('chat', {}).get('id')}:"
            f"{msg.get('message_id')}"
        )
        message_timestamp = msg.get("date")
        requested_at = (
            datetime.fromtimestamp(message_timestamp, tz=timezone.utc)
            if isinstance(message_timestamp, int)
            and not isinstance(message_timestamp, bool)
            else None
        )
        _dispatch(
            command,
            args,
            source_ref=source_ref,
            requested_at=requested_at,
        )
    except Exception as exc:
        logger.warning("handle_message упал: %s", type(exc).__name__)


def dispatch_owner_text(
    text: str,
    *,
    source_ref: str | None = None,
    requested_at: datetime | None = None,
) -> str | None:
    """Исполняет команду пульта из уже доверенного текста владельца.

    Точка входа для единого поллера: проверку владельца делает вызывающий
    (owner-ingress сверяет from.id и chat.id с настройками одобрения), поэтому
    здесь её нет — в отличие от ``handle_message``.

    Возвращает имя исполненной команды или ``None``, если текст командой не
    является. Неизвестную команду отличает вызывающий: ``_parse_command``
    вернёт ``None`` и для «/чтототакое», и для обычного текста.
    """
    command = _parse_command(text)
    if command is None:
        return None
    _dispatch(
        command,
        _parse_args(text),
        source_ref=source_ref,
        requested_at=requested_at,
    )
    return command


def is_known_command(text: str) -> bool:
    """Известна ли команда пульту (для ответа «не знаю такую команду»)."""
    return _parse_command(text) is not None


def _is_owner(msg: dict) -> bool:
    """True если msg от владельца: from.id == chat.id == int(TELEGRAM_CHAT_ID)."""
    try:
        from config import TELEGRAM_CHAT_ID

        if not TELEGRAM_CHAT_ID:
            # Бот не настроен на владельца — безопасный дефолт: никому не отвечаем.
            return False

        from_id = str(msg.get("from", {}).get("id", ""))
        chat_id = str(msg.get("chat", {}).get("id", ""))
        owner_id = str(TELEGRAM_CHAT_ID)

        return bool(from_id) and from_id == chat_id == owner_id
    except Exception as exc:
        logger.warning("_is_owner упал: %s", type(exc).__name__)
        return False


def _parse_command(text: str | None) -> str | None:
    """Достаёт команду из текста сообщения.

    "/status" -> "status"; "/help@yuko_bot" -> "help"; "/scale now" -> "scale".
    Возвращает None если текст не начинается с '/' или команда неизвестна
    (не входит в _COMMANDS).
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip()
    if not text.startswith("/"):
        return None

    # Берём первое "слово" без "/", отсекаем @botname и аргументы
    first_token = text[1:].split()[0] if len(text) > 1 else ""
    command = first_token.split("@")[0].lower()

    if command not in _COMMANDS:
        return None

    return command


def _parse_args(text: str | None) -> str:
    """Возвращает строку аргументов после команды.

    "/hourly 123" -> "123"; "/hourly" -> ""; "/status" -> "".
    text уже прошёл _parse_command (начинается с '/', известная команда).
    """
    if not text or not isinstance(text, str):
        return ""
    parts = text.strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _dispatch(
    command: str,
    args: str = "",
    *,
    source_ref: str | None = None,
    requested_at: datetime | None = None,
) -> None:
    """Маршрутизирует команду в обработчик (см. _COMMANDS).

    status/queue/ads/products/hourly/help — синхронно (быстрые срезы, ≤ пары секунд).
    scale/launch/brief — мгновенный ack + запуск в отдельном потоке.
    args — строка аргументов после команды (нужна /hourly для формы «/hourly <ad_id>»).
    """
    if command == "help":
        _cmd_help()
    elif command == "status":
        _cmd_status()
    elif command == "queue":
        _cmd_queue()
    elif command == "ads":
        _cmd_ads()
    elif command == "scale":
        _cmd_scale(source_ref=source_ref, requested_at=requested_at)
    elif command == "launch":
        _cmd_launch(
            source_ref=source_ref,
            requested_card_id=args,
            requested_at=requested_at,
        )
    elif command == "pause":
        _cmd_status_proposal(
            args,
            pause=True,
            source_ref=source_ref,
            requested_at=requested_at,
        )
    elif command == "unpause":
        _cmd_status_proposal(
            args,
            pause=False,
            source_ref=source_ref,
            requested_at=requested_at,
        )
    elif command == "digest":
        _cmd_digest()
    elif command == "products":
        _cmd_products()
    elif command == "brief":
        _cmd_brief()
    elif command == "hourly":
        _cmd_hourly(args)
    else:
        # Не должно случиться — _parse_command уже отфильтровал по _COMMANDS.
        logger.warning("_dispatch: неизвестная команда после _parse_command: %s", command)


def _owner_repository():
    """Строит repository только из валидированной owner-конфигурации."""

    from config import load_owner_approval_config
    from services.owner_action_repository import OwnerActionRepository

    settings = load_owner_approval_config()
    return OwnerActionRepository(settings.db_path)


def _proposal_source(source_ref: str | None) -> str:
    if isinstance(source_ref, str) and source_ref.strip():
        return source_ref
    return f"telegram-command:{uuid.uuid4()}"


def _persist_telegram_proposal(
    *,
    proposal_kind,
    subject_id: str,
    payload: Mapping[str, object],
    summary: str,
    source_ref: str | None,
    requested_at: datetime | None = None,
    account_id: str | None = None,
    adset_id: str | None = None,
):
    """Сохраняет ровно один immutable proposal без execution/decision вызовов."""

    from config import FB_ACCOUNT_ID
    from services.owner_action_models import (
        ProposalOrigin,
        ProposedActionPlan,
        ProposedTarget,
        canonical_sha256,
    )

    source = _proposal_source(source_ref)
    now = requested_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("requested_at должен содержать timezone")
    now = now.astimezone(timezone.utc)
    action_kind = {
        "LAUNCH": "CREATE_AD",
        "PAUSE": "PAUSE_AD",
        "UNPAUSE": "UNPAUSE_AD",
        "SCALE": "SET_ADSET_BUDGET",
    }[proposal_kind.value]
    immutable_payload = {
        "schema_version": 1,
        "proposal_kind": proposal_kind.value,
        "origin": ProposalOrigin.TELEGRAM_COMMAND.value,
        "subject_id": subject_id,
        "request": dict(payload),
    }
    payload_sha256 = canonical_sha256(immutable_payload)
    idempotency_key = (
        f"{source}:{proposal_kind.value.lower()}:{subject_id}"
    )
    plan = ProposedActionPlan(
        proposal_kind=proposal_kind,
        origin=ProposalOrigin.TELEGRAM_COMMAND,
        idempotency_key=idempotency_key,
        source_ref=source,
        actor="telegram_owner_command",
        summary=summary,
        targets=(
            ProposedTarget(
                claim_id=f"telegram-{payload_sha256[:24]}",
                ordinal=0,
                action_kind=action_kind,
                account_id=str(account_id or FB_ACCOUNT_ID or "default"),
                adset_id=adset_id,
                subject_id=subject_id,
                city=None,
                language=None,
                intended_payload=immutable_payload,
                intended_payload_sha256=payload_sha256,
            ),
        ),
        evidence=(),
        config_version_sha256=canonical_sha256(
            {
                "schema_version": 1,
                "producer": "telegram_console",
                "proposal_kind": proposal_kind.value,
            }
        ),
        valid_until=now + _PROPOSAL_TTL,
        staged_media_root=None,
    )
    return _owner_repository().propose_action(plan, now=now)


def _deliver_owner_proposals() -> None:
    """Best-effort wake-up; durable DELIVERY_PENDING останется для cron retry."""

    try:
        from services.approval_telegram import deliver_owner_proposals

        deliver_owner_proposals(worker_id="telegram-console", limit=20)
    except Exception as exc:
        logger.warning(
            "Owner proposal сохранён, но доставка отложена: %s",
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Обработчики команд (каждый шлёт готовое сообщение сам)
# ---------------------------------------------------------------------------

def _cmd_help() -> None:
    """Шлёт статичный список команд через send_telegram."""
    try:
        from services.notifications import send_telegram

        lines = ["📟 <b>Команды пульта</b>", ""]
        for name, desc in _COMMANDS.items():
            lines.append(f"/{name} — {html.escape(desc)}")

        send_telegram(_truncate_4096("\n".join(lines)))
    except Exception as exc:
        logger.warning("_cmd_help упал: %s", type(exc).__name__)


def _cmd_digest() -> None:
    """Собирает и шлёт пакет предложений прямо сейчас.

    Повторный вызов безвреден: предложения, уже попавшие в дайджест, отсекает
    ``uq_owner_digest_item_proposal``, а доставленные карточки — состояние
    outbox. Пустая очередь получает явный ответ, а не молчание.
    """
    from services.notifications import send_telegram

    try:
        from services.owner_delivery_outbox import send_owner_digest_now

        run = send_owner_digest_now(worker_id="telegram-console-digest")
    except Exception as exc:
        logger.warning("_cmd_digest упал: %s", type(exc).__name__)
        send_telegram("⚠️ Дайджест собрать не удалось — попробую на ближайшем тике.")
        return
    if run.sent_count == 0 and run.trail_sent_count == 0:
        send_telegram("📭 Предложений нет — очередь пуста.")
        return
    send_telegram(f"📋 Отправил карточек: {run.sent_count}")


def _cmd_status() -> None:
    """Собирает срез 'сейчас' и шлёт одним сообщением (send_telegram).

    Переиспользует готовые сборщики (НЕ копирует их логику):
      - деньги/лиды/квалы/юнитка: evening_report._compute_money_windows +
        evening_report._section_money (двухоконный честный формат).
      - что сделал бот за 24ч: morning_digest._section_actions_24h.
    Обрезает по 4096 (_truncate_4096).
    """
    try:
        from services import evening_report
        from services.approval_checker import check_report
        from services.approval_report import render_checked_report
        from services.approval_telegram import send_checked_report

        now = datetime.now(_TZ_LOCAL)

        report = evening_report.build_evening_report(now)
        request = report["check_request"]
        result = check_report(request, now=now)
        rendered = render_checked_report(request, result)
        send_checked_report(rendered, channel="ads")
    except Exception as exc:
        logger.warning("_cmd_status упал: %s", type(exc).__name__)
        _send_failure("/status")


def _cmd_queue() -> None:
    """Топ-10 незапущенных карточек Trello + пометка первой пятёрки + остаток."""
    try:
        from services.auto_launch import _get_done_list_id, _get_unlaunched_cards
        from services.notifications import send_telegram

        try:
            list_id = _get_done_list_id()
            cards = _get_unlaunched_cards(list_id)
        except Exception as exc:
            logger.warning("_cmd_queue: Trello недоступен — %s", type(exc).__name__)
            send_telegram("⚠️ Очередь недоступна (Trello не отвечает)")
            return

        text = _format_queue(cards, top=_QUEUE_TOP, first_batch=_QUEUE_FIRST_BATCH)
        send_telegram(_truncate_4096(text))
    except Exception as exc:
        logger.warning("_cmd_queue упал: %s", type(exc).__name__)
        _send_failure("/queue")


def _cmd_ads() -> None:
    """Топ-8 активных объявлений по расходу за 7д + inline-кнопки [⏸ Пауза].

    Шлёт через telegram_bot.send_with_buttons (кнопка pause:<ad_id> под каждым).
    AMO-обогащение (qual_pct/romi/payments) НЕ дёргаем — дорого и не нужно для
    быстрого среза расходов (документированное упрощение, см. спеку §10).
    """
    try:
        from services.approval_checker import check_report
        from services.approval_report import render_checked_report
        from services.approval_telegram import send_checked_report
        from services.scorecard import build_scorecard, build_scorecard_request

        now = datetime.now(_TZ_LOCAL)
        scorecard = build_scorecard(days=7, now=now)
        request = build_scorecard_request(scorecard, generated_at=now)
        result = check_report(request, now=now)
        rendered = render_checked_report(request, result)
        send_checked_report(rendered, channel="ads")
    except Exception as exc:
        logger.warning("_cmd_ads упал: %s", type(exc).__name__)
        _send_failure("/ads")


def _cmd_products() -> None:
    """Сводка «какого продукта больше»: активные объявления (шт), расход,
    лиды/квалы/квал%, доля расхода — по 4 продуктам (PRODA/PRODB/СТАРТ/ОБЩАЯ)
    + строка «без разметки», если среди ACTIVE есть объявления без канона в
    target_product (NULL/пусто/старый код до бэкфилла).

    Источник — creative_kb.target_product (см. docs/specs/ARCH-product-tags.md
    §5-6), парсинг имён НЕ используется. spend/leads/quals — агрегат
    последнего синка FB+AMO (creative_kb хранит агрегат синка, а не
    по-дневный ряд — «7д» в заголовке информативно, задокументированное
    упрощение спеки §out of scope, а не фактическая фильтрация по дате).
    """
    try:
        from services.notifications import send_telegram

        try:
            raw_rows = _fetch_product_breakdown()
        except Exception as exc:
            logger.warning("_cmd_products: БД недоступна — %s", type(exc).__name__)
            send_telegram("⚠️ Не смог собрать срез по продуктам: нет данных")
            return

        summary = _build_products_summary(raw_rows)
        text = _format_products_report(summary)
        send_telegram(_truncate_4096(text))
    except Exception as exc:
        logger.warning("_cmd_products упал: %s", type(exc).__name__)
        _send_failure("/products")


def _fetch_product_breakdown() -> list[dict]:
    """Тянет сырую разбивку ACTIVE-объявлений по target_product из creative_kb.

    Группирует по СЫРОМУ значению target_product (включая NULL/'' и старые
    невалидные коды) — нормализацию в 4 канона + «без разметки» делает
    _build_products_summary (чистая функция, тестируется без БД).

    Returns: [{"product": str | None, "ads_count": int, "spend": float,
               "leads": int, "quals": int}, ...]
    """
    from services.creative_intelligence import _get_connection

    conn = _get_connection()
    try:
        db_rows = conn.execute(
            "SELECT target_product, COUNT(*) AS ads_count, "
            "COALESCE(SUM(spend), 0) AS spend, "
            "COALESCE(SUM(leads), 0) AS leads, "
            "COALESCE(SUM(qual_leads), 0) AS quals "
            "FROM creative_kb WHERE status = 'ACTIVE' "
            "GROUP BY target_product"
        ).fetchall()
    finally:
        conn.close()

    rows = []
    for r in db_rows:
        rows.append({
            "product": r["target_product"] or None,
            "ads_count": int(r["ads_count"] or 0),
            "spend": float(r["spend"] or 0.0),
            "leads": int(r["leads"] or 0),
            "quals": int(r["quals"] or 0),
        })
    return rows


def _cmd_hourly(args: str) -> None:
    """/hourly — список объявлений младше 48ч с собранными часами;
    /hourly <ad_id> — почасовая таблица (час | $ | показы | клики | лиды) по ad_id.

    Читает ad_hourly_metrics (+ creative_kb для имени/created_at). Чистый срез
    данных почасового сборщика (services/hourly_collector.py) — ничего не мутирует.
    Обрезка по границе слова (truncate_at_word_boundary) до лимита Telegram.
    """
    try:
        from services.formatting import truncate_at_word_boundary
        from services.notifications import send_telegram

        ad_id = (args or "").strip()
        try:
            if ad_id:
                text = _format_hourly_detail(ad_id, _fetch_hourly_detail(ad_id))
            else:
                text = _format_hourly_list(_fetch_hourly_list())
        except Exception as exc:
            logger.warning("_cmd_hourly: срез недоступен — %s", type(exc).__name__)
            send_telegram("⚠️ Не смог собрать почасовой срез: нет данных")
            return

        send_telegram(truncate_at_word_boundary(text, _TELEGRAM_MAX_LEN))
    except Exception as exc:
        logger.warning("_cmd_hourly упал: %s", type(exc).__name__)
        _send_failure("/hourly")


def _fetch_hourly_list(now_local: datetime | None = None) -> list[dict]:
    """Объявления младше 48ч (creative_kb.created_at) с собранными часами
    (строки в ad_hourly_metrics): имя, число собранных часов, суммарные расход/лиды.

    Возраст считаем локально по created_at (как hourly_collector) — объявления с
    пустым/битым created_at в список не попадают (нельзя доказать «младше 48ч»).
    Returns: list[dict], отсортирован по числу собранных часов DESC.
    """
    from services.creative_intelligence import _get_connection

    now = now_local or datetime.now(_TZ_LOCAL)
    now_utc = now.astimezone(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(hours=48)

    conn = _get_connection()
    try:
        db_rows = conn.execute(
            """
            SELECT k.ad_id AS ad_id, k.ad_name AS ad_name, k.created_at AS created_at,
                   COUNT(h.id) AS hours_collected,
                   COALESCE(SUM(h.spend), 0) AS spend,
                   COALESCE(SUM(h.actions_lead), 0) AS leads
            FROM ad_hourly_metrics h
            JOIN creative_kb k ON k.ad_id = h.ad_id
            WHERE k.created_at IS NOT NULL AND k.created_at != ''
              AND h.lead_semantics_version = 2
              AND h.lead_parse_status IN ('ok', 'component_mismatch')
            GROUP BY h.ad_id
            ORDER BY hours_collected DESC
            """
        ).fetchall()
    finally:
        conn.close()

    result: list[dict] = []
    for r in db_rows:
        try:
            created_dt = datetime.fromisoformat(str(r["created_at"]).replace("+0000", "+00:00"))
            created_utc = created_dt.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, AttributeError):
            continue
        if created_utc < cutoff:
            continue  # старше 48ч — не показываем
        result.append({
            "ad_id": r["ad_id"],
            "ad_name": r["ad_name"] or r["ad_id"],
            "hours_collected": int(r["hours_collected"] or 0),
            "spend": float(r["spend"] or 0.0),
            "leads": int(r["leads"] or 0),
        })
    return result


def _fetch_hourly_detail(ad_id: str) -> tuple[str, list[dict]]:
    """Почасовой ряд одного объявления из ad_hourly_metrics (по возрастанию часа).

    Returns: (ad_name, [{"datetime_hour","spend","impressions","clicks","leads"}, ...]).
    ad_name — из creative_kb (или сам ad_id, если строки/имени нет).
    """
    from services.creative_intelligence import _get_connection

    conn = _get_connection()
    try:
        name_row = conn.execute(
            "SELECT ad_name FROM creative_kb WHERE ad_id = ?", (ad_id,)
        ).fetchone()
        db_rows = conn.execute(
            """
            SELECT datetime_hour, spend, impressions, clicks, actions_lead
            FROM ad_hourly_metrics
            WHERE ad_id = ?
              AND lead_semantics_version = 2
              AND lead_parse_status IN ('ok', 'component_mismatch')
            ORDER BY datetime_hour ASC
            """,
            (ad_id,),
        ).fetchall()
    finally:
        conn.close()

    ad_name = name_row["ad_name"] if name_row and name_row["ad_name"] else ad_id
    hours = [{
        "datetime_hour": r["datetime_hour"],
        "spend": float(r["spend"] or 0.0),
        "impressions": int(r["impressions"] or 0),
        "clicks": int(r["clicks"] or 0),
        "leads": int(r["actions_lead"] or 0),
    } for r in db_rows]
    return ad_name, hours


def _short_hour_label(datetime_hour: str) -> str:
    """'2026-07-05T08:00:00' -> '07-05 08'. Неожиданный формат — исходная строка."""
    try:
        date_part, time_part = str(datetime_hour).split("T")
        return f"{date_part[5:]} {time_part[:2]}"
    except (ValueError, AttributeError, IndexError):
        return str(datetime_hour)


def _format_hourly_list(items: list[dict]) -> str:
    """Список объявлений младше 48ч с собранными часами. Пусто → «данных пока нет»."""
    from services.formatting import fmt_money

    if not items:
        return "🕐 <b>Почасовой сбор (младше 48ч)</b>\n\nданных пока нет"

    lines = ["🕐 <b>Почасовой сбор (младше 48ч)</b>", ""]
    for it in items:
        name = html.escape((it["ad_name"] or "")[:50])
        spend = fmt_money(it["spend"], "$")
        lines.append(
            f"• {name}\n"
            f"   {it['hours_collected']}ч · 💸 {spend} · лиды {it['leads']} · "
            f"<code>{html.escape(str(it['ad_id']))}</code>"
        )
    lines.append("")
    lines.append("подробно: /hourly &lt;ad_id&gt;")
    return "\n".join(lines)


def _format_hourly_detail(ad_id: str, data: tuple[str, list[dict]]) -> str:
    """Почасовая таблица (час | $ | показы | клики | лиды) по ad_id.
    Строки — простой текст (без <pre>), безопасно режутся по границе слова."""
    from services.formatting import fmt_money

    ad_name, hours = data
    header = (
        f"🕐 <b>Почасовой ряд: {html.escape((ad_name or ad_id)[:60])}</b>\n"
        f"<code>{html.escape(str(ad_id))}</code>"
    )
    if not hours:
        return header + "\n\nданных пока нет"

    lines = [header, "", "час | $ | показы | клики | лиды"]
    for h in hours:
        label = _short_hour_label(h["datetime_hour"])
        spend = fmt_money(h["spend"], "$")
        lines.append(f"{label} | {spend} | {h['impressions']} | {h['clicks']} | {h['leads']}")
    return "\n".join(lines)


def _cmd_scale(
    *,
    source_ref: str | None = None,
    requested_at: datetime | None = None,
) -> None:
    """Мгновенный ack + read-only подбор SCALE proposals в фоне."""
    try:
        from services.approval_checker_models import FactFreeTemplate
        from services.approval_telegram import send_fact_free

        send_fact_free(FactFreeTemplate.ACTION_CHECK_STARTED, channel="ads")
        threading.Thread(
            target=_run_scale_async,
            kwargs={
                "source_ref": source_ref,
                "requested_at": requested_at,
            },
            daemon=True,
        ).start()
    except Exception as exc:
        logger.warning("_cmd_scale упал: %s", type(exc).__name__)
        _send_failure("/scale")


def _cmd_launch(
    *,
    source_ref: str | None = None,
    requested_card_id: str = "",
    requested_at: datetime | None = None,
) -> None:
    """Мгновенный ack + read-only подбор LAUNCH proposals в фоне."""
    try:
        from services.approval_checker_models import FactFreeTemplate
        from services.approval_telegram import send_fact_free

        send_fact_free(FactFreeTemplate.ACTION_CHECK_STARTED, channel="ads")
        threading.Thread(
            target=_run_launch_async,
            kwargs={
                "source_ref": source_ref,
                "requested_card_id": requested_card_id,
                "requested_at": requested_at,
            },
            daemon=True,
        ).start()
    except Exception as exc:
        logger.warning("_cmd_launch упал: %s", type(exc).__name__)
        _send_failure("/launch")


def _cmd_brief() -> None:
    """Мгновенный ack + запуск генерации ТЗ в фоне (не блокирует поллер).

    Мастер-выключатель НЕ проверяем — это ручная команда владельца (как
    эндпоинт /api/autopilot/generate-briefs-now). Двойной гейт качества и
    очередь одобрения (ARCH-brief-approval-flow.md) отрабатывают как обычно —
    команда лишь триггерит прогон.
    """
    try:
        from services.notifications import send_telegram

        send_telegram("⏳ Генерирую ТЗ…")
        threading.Thread(target=_run_brief_async, daemon=True).start()
    except Exception as exc:
        logger.warning("_cmd_brief упал: %s", type(exc).__name__)
        _send_failure("/brief")


def _run_scale_async(
    *,
    source_ref: str | None = None,
    requested_at: datetime | None = None,
) -> None:
    """Собирает dry-run рекомендации и сохраняет SCALE proposals."""
    try:
        from services.budget_scaler import get_scale_config, run_budget_scaling
        from services.owner_action_models import ProposalKind

        max_scales = int(get_scale_config().get("max_scales_per_run", 2))
        result = run_budget_scaling(mode="dry_run", max_scales=max_scales)
        created = 0
        for record in (result.get("recommendations") or ())[:max_scales]:
            adset_id = str(record.get("adset_id") or "")
            if not adset_id:
                continue
            _persist_telegram_proposal(
                proposal_kind=ProposalKind.SCALE,
                subject_id=adset_id,
                adset_id=adset_id,
                payload=record,
                summary=(
                    "Масштабировать бюджет "
                    f"{record.get('adset_name') or adset_id}"
                ),
                source_ref=source_ref,
                requested_at=requested_at,
            )
            created += 1
        if created:
            _deliver_owner_proposals()
    except Exception as exc:
        logger.warning("_run_scale_async упал: %s", type(exc).__name__)
        try:
            from services.approval_checker_models import FactFreeTemplate
            from services.approval_telegram import send_fact_free
            send_fact_free(
                FactFreeTemplate.CHECKER_INTERNAL_ERROR,
                channel="ads",
                error_type=type(exc).__name__,
            )
        except Exception as exc2:
            logger.warning("_run_scale_async: не смог отправить ошибку — %s", type(exc2).__name__)


def _run_launch_async(
    *,
    source_ref: str | None = None,
    requested_card_id: str = "",
    requested_at: datetime | None = None,
) -> None:
    """Собирает dry-run рекомендации и сохраняет LAUNCH proposals."""
    try:
        from services.auto_launch import _get_autopilot_config, run_auto_launch
        from services.owner_action_models import ProposalKind

        max_launches = int(_get_autopilot_config().get("max_launches_per_day", 1))
        result = run_auto_launch(mode="dry_run", max_launches=max_launches)
        recommendations = list(result.get("recommendations") or ())
        if requested_card_id:
            recommendations = [
                record
                for record in recommendations
                if str(record.get("card_id") or "") == requested_card_id
            ]
        created = 0
        for record in recommendations[:max_launches]:
            card_id = str(record.get("card_id") or "")
            if not card_id:
                continue
            _persist_telegram_proposal(
                proposal_kind=ProposalKind.LAUNCH,
                subject_id=card_id,
                payload=record,
                summary=f"Запустить карточку {record.get('card_name') or card_id}",
                source_ref=source_ref,
                requested_at=requested_at,
            )
            created += 1
        if created:
            _deliver_owner_proposals()
    except Exception as exc:
        logger.warning("_run_launch_async упал: %s", type(exc).__name__)
        try:
            from services.approval_checker_models import FactFreeTemplate
            from services.approval_telegram import send_fact_free
            send_fact_free(
                FactFreeTemplate.CHECKER_INTERNAL_ERROR,
                channel="ads",
                error_type=type(exc).__name__,
            )
        except Exception as exc2:
            logger.warning("_run_launch_async: не смог отправить ошибку — %s", type(exc2).__name__)


def _run_brief_async() -> None:
    """Фоновый воркер /brief: generate_and_push_briefs(max_briefs=3), затем
    send_telegram со сводкой (_format_brief_summary).

    Импорт generate_and_push_briefs — ленивый (как run_budget_scaling в
    _run_scale_async), чтобы не тянуть brief_generator (и его зависимости LLM)
    на уровне модуля telegram_console.
    """
    try:
        from services.brief_generator import generate_and_push_briefs
        from services.notifications import send_telegram

        result = generate_and_push_briefs(max_briefs=3)
        send_telegram(_format_brief_summary(result))
    except Exception as exc:
        logger.warning("_run_brief_async упал: %s", type(exc).__name__)
        try:
            from services.notifications import send_telegram
            send_telegram(f"❌ не получилось сгенерировать ТЗ: {type(exc).__name__}")
        except Exception as exc2:
            logger.warning("_run_brief_async: не смог отправить ошибку — %s", type(exc2).__name__)


def _cmd_status_proposal(
    ad_id: str,
    *,
    pause: bool,
    source_ref: str | None,
    requested_at: datetime | None = None,
) -> None:
    """Создаёт PAUSE/UNPAUSE proposal из явной текстовой команды."""

    command = "/pause" if pause else "/unpause"
    if _AD_ID_RE.fullmatch(ad_id) is None:
        _send_failure(command)
        return
    try:
        from services.owner_action_models import ProposalKind

        proposal_kind = ProposalKind.PAUSE if pause else ProposalKind.UNPAUSE
        meta = _ADS_CACHE.get(ad_id) or {"name": ad_id}
        _persist_telegram_proposal(
            proposal_kind=proposal_kind,
            subject_id=ad_id,
            adset_id=(
                str(meta["adset_id"])
                if meta.get("adset_id")
                else None
            ),
            payload={
                "ad_id": ad_id,
                "requested_status": "PAUSED" if pause else "ACTIVE",
                "display_name": str(meta.get("name") or ad_id),
            },
            summary=(
                f"Поставить на паузу {meta.get('name') or ad_id}"
                if pause
                else f"Вернуть {meta.get('name') or ad_id}"
            ),
            source_ref=source_ref,
            requested_at=requested_at,
        )
        _deliver_owner_proposals()
    except Exception as exc:
        logger.warning("%s proposal не создан: %s", command, type(exc).__name__)
        _send_failure(command)


def execute_owner_pause(ad_id: str, callback_query_id: str) -> None:
    """Совместимый legacy entry point: старая кнопка всегда протухла."""

    from services.telegram_bot import _answer_callback

    _answer_callback(
        callback_query_id,
        "Кнопка устарела — используй /pause <ad_id>",
    )


def _send_failure(command: str) -> None:
    """Шлёт владельцу явное сообщение о провале команды (не молчим на команде владельца)."""
    try:
        from services.notifications import send_telegram
        send_telegram(f"⚠️ Команда {html.escape(command)} упала")
    except Exception as exc:
        logger.warning("_send_failure упал: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Форматтеры (чистые функции, легко тестируются)
# ---------------------------------------------------------------------------

def _format_scale_summary(result: dict) -> str:
    """result = run_budget_scaling(...). Человеческий текст: подняли N адсетов
    (или 'пропущено: <skipped_reason>'), топ-3 строкой, HTML ≤ 4096.
    """
    from services.formatting import fmt_money

    skipped_reason = result.get("skipped_reason")
    if skipped_reason:
        return _truncate_4096(f"⚖️ <b>Масштабирование</b>\n\nпропущено: {html.escape(str(skipped_reason))}")

    scaled = result.get("scaled") or []
    if not scaled:
        return _truncate_4096("⚖️ <b>Масштабирование</b>\n\nничего не подняли (нет кандидатов)")

    lines = ["⚖️ <b>Масштабирование</b>", "", f"подняли {len(scaled)} адсетов:"]
    for rec in scaled[:3]:
        name = html.escape(rec.get("adset_name") or rec.get("ad_name") or "—")
        current = fmt_money(rec.get("current_budget_usd"), "$")
        new = fmt_money(rec.get("new_budget_usd"), "$")
        lines.append(f"• {name}: {current} → {new}")

    unit_source = result.get("unit_source")
    if unit_source:
        lines.append("")
        lines.append(f"источник юнитки: {html.escape(str(unit_source))}")

    return _truncate_4096("\n".join(lines))


def _format_launch_summary(result: dict) -> str:
    """result = run_auto_launch(...). Человеческий текст: запущено N (или
    'пропущено: <skipped_reason>' / 'дневной лимит исчерпан'), HTML ≤ 4096.
    """
    skipped_reason = result.get("skipped_reason")
    if skipped_reason:
        return _truncate_4096(f"🚀 <b>Авто-запуск</b>\n\nпропущено: {html.escape(str(skipped_reason))}")

    error = result.get("error")
    launched = result.get("launched") or []

    if not launched:
        text = "🚀 <b>Авто-запуск</b>\n\nничего не запустили (нет подходящих карточек)"
        if error:
            text += f"\nошибка: {html.escape(str(error))}"
        return _truncate_4096(text)

    lines = ["🚀 <b>Авто-запуск</b>", "", f"запущено {len(launched)}:"]
    for rec in launched[:3]:
        name = html.escape(rec.get("card_name") or rec.get("card_id") or "—")
        lines.append(f"• {name}")
    if error:
        lines.append("")
        lines.append(f"ошибка (частично): {html.escape(str(error))}")

    return _truncate_4096("\n".join(lines))


def _format_brief_summary(result: dict) -> str:
    """result = generate_and_push_briefs(...). Человеческий текст: сколько ТЗ
    отправлено владельцу на одобрение / всё уже было / заблокировано
    проверками, HTML ≤ 4096 (ARCH-brief-approval-flow.md)."""
    queued = result.get("queued", result.get("created", 0)) or 0
    blocked = result.get("blocked", 0) or 0
    error = result.get("error")

    if queued > 0:
        lines = ["📝 <b>Генерация ТЗ</b>", "",
                 f"отправил {queued} ТЗ тебе на одобрение — жми ✅/❌ под каждым"]
        if blocked:
            lines.append(f"заблокировано проверками: {blocked}")
        return _truncate_4096("\n".join(lines))
    if blocked > 0:
        return _truncate_4096(f"📝 <b>Генерация ТЗ</b>\n\nвсе {blocked} ТЗ заблокированы проверками — "
                              "ничего не отправил")
    if error:
        return _truncate_4096(f"📝 <b>Генерация ТЗ</b>\n\n{html.escape(str(error))}")
    return _truncate_4096("📝 <b>Генерация ТЗ</b>\n\nновых тем нет — всё уже было")


def _format_queue(cards: list[dict], top: int = 10, first_batch: int = 5) -> str:
    """Форматирует список карточек: первые first_batch помечены '⏳ уйдут завтра
    10:00', остаток 'остаток: N'. Чистая функция (список карточек на входе).
    """
    if not cards:
        return "✅ Очередь пуста — незапущенных карточек нет"

    shown = cards[:top]
    remainder = max(len(cards) - len(shown), 0)

    lines = ["🗂 <b>Очередь запусков</b>", ""]
    for i, card in enumerate(shown, start=1):
        name = html.escape(card.get("name", "") or "без названия")
        mark = " ⏳ уйдут завтра 10:00" if i <= first_batch else ""
        lines.append(f"{i}. {name}{mark}")

    if remainder:
        lines.append("")
        lines.append(f"остаток: {remainder}")

    return "\n".join(lines)


def _format_ads_block(ad: dict, index: int) -> str:
    """Один блок объявления для /ads: '{i}. {city} | {name} — 💸 spend · CPL · квал · оплат'."""
    name = ad.get("name", "") or ""
    if len(name) > 60:
        name = name[:57] + "..."
    city = ad.get("city") or "—"

    from services.formatting import fmt_money

    spend_str = fmt_money(ad.get("spend"), "$")
    cpl = ad.get("cpl")
    cpl_str = fmt_money(cpl, "$") if cpl is not None else "—"

    qual_pct = ad.get("qual_pct")
    qual_str = f"{qual_pct:.0f}%" if qual_pct is not None else "—"

    payments = ad.get("payments")
    payments_str = f"{payments}" if payments is not None else "—"

    return (
        f"{index}. <b>{html.escape(city)}</b> | {html.escape(name)}\n"
        f"   💸 {spend_str} · CPL {cpl_str} · квал {qual_str} · оплат {payments_str}"
    )


def _build_products_summary(raw_rows: list[dict]) -> dict:
    """Чистая функция: сырые группы из _fetch_product_breakdown → готовая
    структура для форматтера.

    ВСЕГДА 4 известных продукта (PRODA/PRODB/СТАРТ/ОБЩАЯ), даже с нулями.
    Строки с target_product вне VALID_PRODUCTS (NULL/'' /старый несведённый код)
    агрегируются отдельно в "unmarked" — это "без разметки" в отчёте, не
    один из 4 продуктов (см. docs/specs/ARCH-product-tags.md).

    share_pct — доля расхода среди 4 известных продуктов (unmarked в базу
    доли НЕ входит: он не продукт, а сигнал «требуется бэкфилл»).
    qual_pct — квалов от лидов (SUM(quals)/SUM(leads)*100), None если лидов 0.

    Returns: {"rows": [...4 продукта, отсортировано по ads_count DESC...],
              "unmarked": {"ads_count","spend","leads","quals"} | None,
              "total_ads": int, "total_spend": float}
    """
    from services.product_tags import VALID_PRODUCTS

    by_product = {
        p: {"product": p, "ads_count": 0, "spend": 0.0, "leads": 0, "quals": 0}
        for p in VALID_PRODUCTS
    }
    unmarked = {"ads_count": 0, "spend": 0.0, "leads": 0, "quals": 0}
    has_unmarked = False

    for row in raw_rows:
        product = row.get("product")
        if product in VALID_PRODUCTS:
            bucket = by_product[product]
        else:
            bucket = unmarked
            has_unmarked = True

        bucket["ads_count"] += row.get("ads_count", 0) or 0
        bucket["spend"] += row.get("spend", 0.0) or 0.0
        bucket["leads"] += row.get("leads", 0) or 0
        bucket["quals"] += row.get("quals", 0) or 0

    total_spend = sum(b["spend"] for b in by_product.values())

    rows = []
    for bucket in by_product.values():
        qual_pct = (bucket["quals"] / bucket["leads"] * 100) if bucket["leads"] else None
        share_pct = round(bucket["spend"] / total_spend * 100, 1) if total_spend else 0.0
        rows.append({**bucket, "qual_pct": qual_pct, "share_pct": share_pct})

    rows.sort(key=lambda r: r["ads_count"], reverse=True)

    total_ads = sum(b["ads_count"] for b in by_product.values())

    return {
        "rows": rows,
        "unmarked": unmarked if has_unmarked else None,
        "total_ads": total_ads,
        "total_spend": total_spend,
    }


def _format_products_report(summary: dict) -> str:
    """HTML-строка для /products (≤4096). Чистая функция (см. _truncate_4096
    для обрезки — вызывается снаружи, в _cmd_products).

    '{i}. {ПРОДУКТ} — N объявл. · spend · лиды L · квал Q (qual%) · share% расхода',
    топ-3 по числу активных с медалями 🥇🥈🥉, остальные без медали.
    """
    from services.formatting import fmt_money

    rows = summary["rows"]
    unmarked = summary["unmarked"]

    lines = ["📦 <b>Какого продукта больше</b> (активные, расход — агрегат синка)", ""]

    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows):
        mark = medals[i] if i < len(medals) else "  "
        product = html.escape(str(row["product"]))
        spend_str = fmt_money(row["spend"], "$")
        qual_pct = row["qual_pct"]
        qual_str = f"{qual_pct:.0f}%" if qual_pct is not None else "—"
        lines.append(
            f"{mark} {product} — {row['ads_count']} объявл. · {spend_str} · "
            f"лиды {row['leads']} · квал {row['quals']} ({qual_str}) · {row['share_pct']}% расхода"
        )

    if unmarked and unmarked["ads_count"] > 0:
        lines.append("")
        lines.append(
            f"⚠️ без разметки: {unmarked['ads_count']} объявл. · "
            f"{fmt_money(unmarked['spend'], '$')} (требует бэкфилла target_product)"
        )

    lines.append("")
    lines.append(f"Всего активных: {summary['total_ads']} · расход {fmt_money(summary['total_spend'], '$')}")

    return "\n".join(lines)


def _truncate_4096(text: str) -> str:
    """Обрезает по лимиту Telegram (4096) c пометкой '…(см. дашборд)'."""
    if len(text) <= _TELEGRAM_MAX_LEN:
        return text

    suffix = "\n…(см. дашборд)"
    cut = _TELEGRAM_MAX_LEN - len(suffix)
    return text[:cut] + suffix
