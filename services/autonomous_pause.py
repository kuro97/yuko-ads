"""Автономные паузы подтверждённых сливов — единственный автономный класс.

Решение владельца: боту возвращается автономия, но ровно в одном классе
действий — выключить рекламу, про которую УЖЕ ДОКАЗАНО, что она сливает.
Всё остальное (запуски, подъём и снижение бюджета, паузы по нулевым лидам,
паузы по тренду, портфельные аутсайдеры) остаётся предложением владельцу с
кнопкой в Telegram. Эта граница жёсткая, см. ``AUTONOMOUS_ACTION_KINDS``.

Что здесь есть:
  * определение класса (``select_autonomous_candidates``) — берётся из уже
    существующей логики, ничего нового не изобретается;
  * предохранитель от аномалии в данных (``check_anomaly``);
  * самоодобрение через репозиторий решений (``approve_autonomously``);
  * журнал дня и одна вечерняя сводка с кнопками «↩️ Вернуть».

Чего здесь НЕТ и быть не может: прямых вызовов Facebook. Пауза исполняется
тем же путём, что и одобренная владельцем — очередь заданий, живой перечит
состояния, permit, транспорт с аттестациями, независимая верификация факта.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Mapping

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = _ROOT / "data" / "autonomous_pause_state.json"

# Машинное имя правила: попадает в owner_action_decisions.automation_rule и в
# событие SYSTEM_APPROVE. По нему датасет обучения отделяет автоматические
# решения от одобренных владельцем, а владелец в аудите видит, ПОЧЕМУ реклама
# выключена без его кнопки.
AUTOMATION_RULE = "AUTONOMOUS_PAUSE_CONFIRMED_WASTER"
SYSTEM_ACTOR = "autonomous_pause"

# Ранний стоп (services/early_kill.py): правило
# «расход ≥ N цен лида, заявок в AMO нет» выключает рекламу само, тем же
# классом PAUSE_AD и тем же конвейером. Правило B «зрелый ноль» получит своё
# имя, когда владелец включит его в бой (волна 2c).
EARLY_KILL_AUTOMATION_RULES: dict[str, str] = {
    "A": "EARLY_KILL_SPEND_CPL",      # деньги есть, заявок нет
    "B": "EARLY_KILL_MATURE_ZERO",    # заявки есть, квалов нет (волна 2c)
    "C": "EARLY_KILL_QUALITY",        # квалы дорогие или ниже 10% на объёме (волна 2c)
    "S": "EARLY_KILL_STARVING",       # голодные: слоты и размазанный бюджет (волна 2c)
}
EARLY_KILL_ACTOR = "early_kill"

# ЖЁСТКАЯ ГРАНИЦА автономии. Ровно один класс действий. Любое расширение этого
# набора — отдельное решение владельца, а не рефакторинг: см. тест-страж
# tests/test_autonomous_pause.py::test_no_other_action_class_is_autonomous.
AUTONOMOUS_ACTION_KINDS: frozenset[str] = frozenset({"PAUSE_AD"})

# Предохранитель от аномалии: сколько прогонов держим в истории и при каком
# превышении медианы отказываемся исполнять что-либо.
ANOMALY_HISTORY_RUNS = 14
ANOMALY_MEDIAN_MULT = 3.0
ANOMALY_MIN_COUNT = 10

# Ретеншн журнала дня: сводка нужна за сегодня, но пара суток запаса спасает
# от рассинхрона часовых поясов и от пропущенного вечернего слота.
_JOURNAL_RETENTION_DAYS = 3

AUTONOMOUS_DEFAULTS: dict[str, dict] = {
    "autonomous": {
        # Мастер-ключ автономных пауз. ДЕФОЛТ FALSE: после деплоя поведение не
        # меняется ни на йоту, включает владелец через настройки без деплоя.
        "pause_confirmed_wasters": False,
        # Полная автономия: исполнять ВСЕ pause-кандидаты (решение
        # владельца). ОБЯЗАН быть в дефолтах: get_autopilot_config фильтрует
        # nested-блоки по известным ключам, и отсутствующий здесь ключ молча
        # выбрасывался при чтении — файл говорил true, рантайм видел False
        # (регрессия: несколько дней карточек вместо итогов).
        "pause_all_candidates": False,
        # Предохранитель от аномалии в данных. Дефолт True.
        "anomaly_guard": True,
    },
}


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def autonomous_config(cfg: Mapping[str, object] | None = None) -> dict:
    """Блок autopilot.autonomous с fail-safe чтением.

    ``cfg`` — уже прочитанный ``get_autopilot_config()``; если не передан,
    читаем сами. Любая ошибка чтения даёт пустой блок, то есть выключенную
    автономию: молчаливая деградация здесь идёт в безопасную сторону.
    """
    if cfg is None:
        try:
            from services.autopilot import get_autopilot_config

            cfg = get_autopilot_config() or {}
        except Exception as exc:  # noqa: BLE001 — настройки не роняют прогон
            logger.warning("autonomous_pause: настройки недоступны — %s", exc)
            cfg = {}
    block = (cfg or {}).get("autonomous")
    return block if isinstance(block, dict) else {}


def is_autonomous_pause_enabled(cfg: Mapping[str, object] | None = None) -> bool:
    """Мастер-ключ автономных пауз, fail-CLOSED.

    Проверка ``is True``, а не ``.get(..., False)``: это мутация рекламы за
    деньги владельца. Строка "true", 1, "yes" из правленого руками
    settings.json автономию НЕ включают — только настоящий JSON-boolean true,
    который кладёт туда валидатор настроек. Неизвестное значение = выключено.
    """
    return autonomous_config(cfg).get("pause_confirmed_wasters") is True


def is_full_pause_autonomy_enabled(cfg: Mapping[str, object] | None = None) -> bool:
    """Полная автономия PAUSE: бот исполняет ВСЕ pause-кандидаты автопилота.

    Решение владельца — режим «никаких апрувов, только итоги», принятый
    с пониманием цены ложных срабатываний серой зоны.
    Класс действий не расширяется (только PAUSE_AD, тест-страж прежний),
    предохранитель аномалий и страж последнего живого работают как раньше,
    возврат — кнопкой «Вернуть» в сводке 9:00. Fail-closed: только настоящий
    JSON-boolean true.
    """
    return autonomous_config(cfg).get("pause_all_candidates") is True


def is_anomaly_guard_enabled(cfg: Mapping[str, object] | None = None) -> bool:
    """Предохранитель от аномалии, fail-SAFE (дефолт включён).

    Здесь дисциплина обратная мастер-ключу: выключить защиту можно ТОЛЬКО
    настоящим JSON-boolean false. Любое другое значение (мусор, опечатка,
    отсутствие ключа) оставляет предохранитель включённым.
    """
    return autonomous_config(cfg).get("anomaly_guard") is not False


# ---------------------------------------------------------------------------
# Класс действий: подтверждённый слив
# ---------------------------------------------------------------------------

def is_autonomous_pause_candidate(
    decision: Mapping[str, object],
    ad: Mapping[str, object] | None,
    *,
    use_erp_payments: bool = False,
    cfg_v2: Mapping[str, object] | None = None,
) -> bool:
    """Один кандидат на АВТОНОМНУЮ паузу — пересечение двух живых определений.

    Ничего нового не изобретается, берутся ОБА существующих определения слива,
    и требуется, чтобы сработали ОБА (то есть строже каждого по отдельности):

    1. ``decision_policy`` — флаг ``is_confirmed_waster`` (тир A) из того же
       прогона, который и породил решение PAUSE. Тир A уже требует: сверка с
       AMO прошла (``outcomes_matched_at IS NOT NULL``), оплат ноль, расход
       > $300, лидов ≥ 15, и страховку «квал < 25%» — если лиды квалятся
       хорошо, оплата скорее в лаге, и такую рекламу тир A не трогает.
    2. ``budget_scaler._is_significant_waster`` — сверено И effective
       payments == 0 И расход ≥ ``waster_min_spend_usd`` (дефолт $10 —
       компромисс между шумом и пропуском слива). Здесь важен не столько порог (тир A и так
       требует $300), сколько семантика effective payments: в режиме
       ``cdp.payments_source=erp`` ноль оплат должны подтвердить ОБА источника,
       AMO и ERP. Иначе бот выключил бы рекламу, у которой оплата есть в ERP, но
       ещё не доехала в AMO.

    Автономия обязана быть подмножеством того, что бот и так предлагает: если
    решение не PAUSE, кандидатом оно не станет ни при каких флагах.
    """
    if str(decision.get("action") or "") != "PAUSE":
        return False

    # v2 (по ретро-симуляции на истории): прежняя пара
    # замков «тир A + significant_waster» давала много ложных сработок — квал%
    # считался по незрелым лидам, и реклама, чьи квалы ещё дозревали, выглядела
    # сливом. Правила v2 (services/waster_rules_v2) считают только по зрелому:
    # R1 «зрелый ноль» и R2 «мёртвая тишина» — оба с высокой точностью на истории.
    # Тир A остаётся генератором ПРЕДЛОЖЕНИЙ с кнопкой — сюда решения и
    # приходят; автономия лишь выбирает из них доказуемые по v2. Аргументы
    # ad/use_erp_payments/cfg_v2 сохранены в сигнатуре для совместимости
    # вызова, но вердикт теперь строится на точечных свежих чтениях.
    del ad, use_erp_payments, cfg_v2
    ad_id = str(decision.get("ad_id") or "")
    if not ad_id:
        return False

    from services.waster_rules_v2 import confirmed_waster_v2

    verdict = confirmed_waster_v2(ad_id)
    if verdict.is_waster:
        logger.info(
            "autonomous_pause: %s подтверждён v2 (%s: %s)",
            ad_id,
            verdict.rule,
            verdict.detail,
        )
    return verdict.is_waster


def select_autonomous_candidates(
    decisions: list[dict],
    ads_by_id: Mapping[str, Mapping[str, object]],
    *,
    use_erp_payments: bool = False,
    cfg_v2: Mapping[str, object] | None = None,
) -> set[str]:
    """ad_id тех решений, которые бот имеет право исполнить сам."""
    selected: set[str] = set()
    for decision in decisions:
        ad_id = str(decision.get("ad_id") or "")
        if not ad_id:
            continue
        if is_autonomous_pause_candidate(
            decision,
            ads_by_id.get(ad_id),
            use_erp_payments=use_erp_payments,
            cfg_v2=cfg_v2,
        ):
            selected.add(ad_id)
    return selected


# ---------------------------------------------------------------------------
# Состояние: история прогонов + журнал дня
# ---------------------------------------------------------------------------

def _empty_state() -> dict:
    return {"runs": [], "journal": []}


def load_state() -> dict:
    """Толерантное чтение состояния: битый файл не роняет прогон.

    Потеря истории делает предохранитель строже (медиана пустая → любые ≥10
    кандидатов считаются аномалией), а не слабее — деградация в безопасную
    сторону.
    """
    if not STATE_FILE.exists():
        return _empty_state()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("autonomous_pause: состояние нечитаемо — %s", exc)
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    runs = data.get("runs")
    journal = data.get("journal")
    return {
        "runs": [int(item) for item in runs if isinstance(item, int)]
        if isinstance(runs, list)
        else [],
        "journal": [item for item in journal if isinstance(item, dict)]
        if isinstance(journal, list)
        else [],
    }


def save_state(state: Mapping[str, object]) -> None:
    """Атомарная запись (уникальный temp + fsync + os.replace)."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_name(f"{STATE_FILE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(dict(state), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception as exc:  # noqa: BLE001 — журнал не имеет права ронять паузу
        logger.error("autonomous_pause: состояние не сохранено — %s", exc)
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Предохранитель от аномалии в данных
# ---------------------------------------------------------------------------

def anomaly_verdict(candidate_count: int, history: list[int]) -> tuple[bool, str]:
    """Чистое правило предохранителя: (аномалия?, человеческое объяснение).

    ЭТО ЗАЩИТА ОТ БАГА В ДАННЫХ, А НЕ ОТ РЕШЕНИЙ ВЛАДЕЛЬЦА. Владелец дневной
    лимит автономных пауз отклонил сознательно: если сливов действительно
    много, их и надо выключить. Предохранитель ловит другое — момент, когда
    кандидатов внезапно втрое больше обычного, что на практике означает не
    массовый слив, а сломанный вход: сверка с AMO проставила
    ``outcomes_matched_at`` всем подряд, платежи не доехали, источник оплат
    отдал нули. В такой момент правильное поведение — не выключить ничего и
    разбудить владельца с цифрами.

    Порог: строго больше 3× медианы за последние 14 прогонов И не меньше 10
    штук. Оба условия обязательны, поэтому единичные всплески (1-2 слива при
    медиане 0) через предохранитель проходят.
    """
    if candidate_count < ANOMALY_MIN_COUNT:
        return False, ""
    window = [int(item) for item in history][-ANOMALY_HISTORY_RUNS:]
    baseline = float(median(window)) if window else 0.0
    if candidate_count <= ANOMALY_MEDIAN_MULT * baseline:
        return False, ""
    return True, (
        f"кандидатов на автопаузу {candidate_count}, "
        f"медиана за последние {len(window) or 0} прогонов — {baseline:.1f} "
        f"(порог: больше {ANOMALY_MEDIAN_MULT:.0f}× медианы и не меньше "
        f"{ANOMALY_MIN_COUNT})"
    )


def check_anomaly(candidate_count: int, *, enabled: bool = True) -> tuple[bool, str]:
    """Проверяет прогон против своей истории и обновляет её.

    История пополняется ТОЛЬКО нормальными прогонами: записать аномальный
    выброс значило бы поднять медиану и пропустить такой же выброс завтра.
    """
    state = load_state()
    history = list(state.get("runs") or [])
    if not enabled:
        # Предохранитель выключен настройкой: историю всё равно ведём, чтобы
        # обратное включение сразу работало на живых данных.
        state["runs"] = (history + [int(candidate_count)])[-ANOMALY_HISTORY_RUNS:]
        save_state(state)
        return False, ""
    is_anomaly, detail = anomaly_verdict(candidate_count, history)
    if not is_anomaly:
        state["runs"] = (history + [int(candidate_count)])[-ANOMALY_HISTORY_RUNS:]
        save_state(state)
    return is_anomaly, detail


def send_anomaly_alert(detail: str) -> None:
    """Критический алерт «похоже на сбой данных». Не роняет прогон."""
    try:
        from services.notifications import send_critical_alert

        send_critical_alert(
            "Автономные паузы остановлены: похоже на сбой данных",
            f"{detail}\n\nНичего не выключено. Проверьте сверку с AMO и источник оплат.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("autonomous_pause: алерт об аномалии не ушёл — %s", exc)


# ---------------------------------------------------------------------------
# Самоодобрение
# ---------------------------------------------------------------------------

def approve_autonomously(
    proposal_id: str,
    *,
    evidence: Mapping[str, object] | None = None,
    now: datetime | None = None,
) -> bool:
    """Помечает предложение как принятое ботом и ставит его в очередь исполнения.

    Возвращает False при любом отказе репозитория (предложение уже уехало
    владельцу, уже есть решение, истёк TTL). Отказ безопасен: предложение
    остаётся обычным и владелец увидит его карточку с кнопками.
    """
    from services.owner_action_repository import approve_by_system

    try:
        approve_by_system(
            proposal_id=proposal_id,
            automation_rule=AUTOMATION_RULE,
            actor=SYSTEM_ACTOR,
            reason_text=str((evidence or {}).get("business_reason") or "") or None,
            evidence=evidence,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — отказ = обычное предложение владельцу
        logger.warning(
            "autonomous_pause: самоодобрение %s отклонено (%s) — "
            "предложение уходит владельцу как обычно",
            proposal_id,
            type(exc).__name__,
        )
        return False
    return True


def approve_early_kill(
    proposal_id: str,
    *,
    rule: str,
    evidence: Mapping[str, object] | None = None,
    now: datetime | None = None,
) -> bool:
    """Самоодобрение паузы раннего стопа (правило A/B) — та же дисциплина, что выше.

    Отдельная точка нужна, чтобы в аудите (`automation_rule`) было видно,
    какое именно правило выключило рекламу. Возвращает False при любом отказе
    репозитория: предложение тогда остаётся обычной карточкой владельцу.
    """
    from services.owner_action_repository import approve_by_system

    automation_rule = EARLY_KILL_AUTOMATION_RULES.get(str(rule))
    if automation_rule is None:
        logger.warning("early_kill: неизвестное правило %r — самоодобрение отклонено", rule)
        return False
    try:
        approve_by_system(
            proposal_id=proposal_id,
            automation_rule=automation_rule,
            actor=EARLY_KILL_ACTOR,
            reason_text=str((evidence or {}).get("business_reason") or "") or None,
            evidence=evidence,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — отказ = обычное предложение владельцу
        logger.warning(
            "early_kill: самоодобрение %s отклонено (%s) — предложение уходит владельцу",
            proposal_id,
            type(exc).__name__,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Журнал дня и вечерняя сводка
# ---------------------------------------------------------------------------

def record_autonomous_pause(entry: Mapping[str, object], *, now: datetime | None = None) -> None:
    """Пишет автономную паузу в журнал дня (для утренней сводки).

    Одна реклама — одна запись: пока конвейер довозит физическую паузу,
    следующие слоты самоодобряют то же решение повторно, и без дедупа журнал
    копил по несколько записей на рекламу (сводка превращалась в «солянку»).
    Повтор ЗАМЕНЯЕТ запись (свежие цифры расхода побеждают), а не дописывает.
    """
    moment = now or datetime.now(timezone.utc)
    state = load_state()
    journal = [item for item in (state.get("journal") or []) if isinstance(item, dict)]
    cutoff = (moment - timedelta(days=_JOURNAL_RETENTION_DAYS)).date().isoformat()
    journal = [item for item in journal if str(item.get("at") or "")[:10] >= cutoff]
    ad_id = str(dict(entry).get("ad_id") or "")
    if ad_id:
        journal = [
            item for item in journal if str(item.get("ad_id") or "") != ad_id
        ]
    journal.append({**dict(entry), "at": moment.isoformat()})
    state["journal"] = journal
    save_state(state)


def autonomous_pauses_for_day(day: str) -> list[dict]:
    """Записи журнала за конкретную дату (YYYY-MM-DD)."""
    state = load_state()
    return [
        item
        for item in (state.get("journal") or [])
        if isinstance(item, dict) and str(item.get("at") or "").startswith(day)
    ]


def format_daily_summary(entries: list[dict]) -> str:
    """Текст вечерней сводки: что бот выключил сам и сколько это стоило.

    Формат — эталон проекта (``autopilot._format_live_pause_block``): деньги
    через ``fmt_money``, None печатаем как «нет данных», а не как 0.

    Не влезло в лимит Telegram — режем целыми блоками с конца и добавляем
    «…и ещё N (см. дашборд)», как ``autopilot._format_pause_report``. Лимит
    жёсткий: сообщение длиннее 4096 API отклоняет целиком, и владелец не
    узнал бы ни об одной паузе. Счётчик в шапке остаётся полным — выключено
    столько, сколько выключено, сколько бы блоков ни поместилось.
    """
    import html

    from services.autopilot import _TELEGRAM_MAX_LEN, _format_live_pause_block
    from services.formatting import fmt_money, truncate_at_word_boundary

    total_spend = sum(float(item.get("spend") or 0.0) for item in entries)
    header = (
        f"🤖 <b>Выключил сам: {len(entries)}</b>\n"
        f"Подтверждённый слив — сверка с AMO прошла, оплат ноль.\n"
        f"Освободили {fmt_money(total_spend, '$')} дневного слива."
    )
    blocks = [
        _format_live_pause_block(index, entry)
        for index, entry in enumerate(entries, start=1)
    ]
    footer = html.escape("Не согласны — «↩️ Вернуть», предложу возврат.")
    if not blocks:
        return "\n\n".join([header, footer])

    for cut in range(len(blocks), 0, -1):
        omitted = len(blocks) - cut
        parts = [header, *blocks[:cut]]
        if omitted:
            parts.append(f"…и ещё {omitted} (см. дашборд)")
        parts.append(footer)
        text = "\n\n".join(parts)
        if len(text) <= _TELEGRAM_MAX_LEN:
            return text

    # Даже один блок не влез (аномально длинное имя) — режем его по слову.
    parts = [header, truncate_at_word_boundary(blocks[0], _TELEGRAM_MAX_LEN // 2)]
    if len(blocks) > 1:
        parts.append(f"…и ещё {len(blocks) - 1} (см. дашборд)")
    parts.append(footer)
    return "\n\n".join(parts)


def autonomous_pauses_since(since_iso: str) -> list[dict]:
    """Записи журнала строго после метки ``since_iso`` (ISO UTC).

    Для утренней сводки 9:00: окно «с прошлой сводки», а не календарный день —
    иначе паузы вечерних слотов (14–22) не попадали бы ни в одну сводку.
    """
    state = load_state()
    return [
        item
        for item in (state.get("journal") or [])
        if isinstance(item, dict) and str(item.get("at") or "") > since_iso
    ]


def _confirmed_pause_ids(since_iso: str) -> set[str] | None:
    """ad_id пауз, которые контур одобрений физически подтвердил после ``since_iso``.

    None — сверить нечем (БД не инициализирована или таблицы контура нет).
    """
    try:
        import sqlite3

        from services.creative_intelligence import DB_PATH

        if DB_PATH is None:
            return None
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT resource_id FROM owner_action_attempts
                WHERE operation_kind = 'PAUSE_AD' AND state = 'CONFIRMED'
                  AND completed_at > ?
                """,
                (since_iso,),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()
        return {str(row[0]) for row in rows if row[0]}
    except Exception as exc:  # noqa: BLE001 — сверка не роняет сводку
        logger.warning("autonomous_pause: сверка исполнения упала — %s", exc)
        return None


def send_daily_summary(now: datetime | None = None, *, since: str | None = None) -> bool:
    """Одно сообщение в день про автономные паузы. Пусто — не шлём вообще.

    Кнопка «↩️ Вернуть» на каждую паузу — тот же callback ``undo_pause:<ad_id>``,
    что и в отчётах автопилота: он ничего не мутирует, а создаёт предложение
    возврата владельцу (``action_producer_gateway.propose_unpause``). То есть
    выключение автономно, а возврат — по-прежнему решение владельца.

    ``since`` (ISO UTC) — окно «с прошлой сводки»; без него — календарный день
    ``now`` (легаси-поведение для ручных вызовов).
    """
    moment = now or datetime.now(timezone.utc)
    if since:
        entries = autonomous_pauses_since(since)
        window_start = since
    else:
        entries = autonomous_pauses_for_day(moment.date().isoformat())
        window_start = f"{moment.date().isoformat()}T00:00:00+00:00"
    # Страховочный дедуп по ad_id (журналы до фикса могли накопить повторы):
    # последняя запись побеждает; крупные расходы — в начало сводки.
    latest: dict[str, dict] = {}
    for item in entries:
        key = str(item.get("ad_id") or "") or f"__norec_{id(item)}"
        latest[key] = item
    entries = sorted(
        latest.values(),
        key=lambda item: float(item.get("spend") or 0.0),
        reverse=True,
    )
    # Журнал пишется в момент самоодобрения, а физическую паузу довозит
    # конвейер — и может не довезти (регрессия: записи «выключено» при
    # по-прежнему живых рекламах). Сводка — отчёт о ФАКТЕ: оставляем только то,
    # что контур подтвердил (owner_action_attempts CONFIRMED). None = сверить
    # нечем (нет БД/таблицы — тестовые среды): шлём как раньше, с предупреждением.
    confirmed = _confirmed_pause_ids(window_start)
    if confirmed is None:
        logger.warning("autonomous_pause: сверка исполнения недоступна — сводка по журналу")
    else:
        unconfirmed = [
            str(item.get("ad_id") or "") for item in entries
            if str(item.get("ad_id") or "") and str(item.get("ad_id")) not in confirmed
        ]
        if unconfirmed:
            logger.warning(
                "autonomous_pause: %d записей журнала без подтверждённой паузы — в сводку не идут: %s",
                len(unconfirmed),
                unconfirmed,
            )
        entries = [item for item in entries if str(item.get("ad_id") or "") in confirmed]
    if not entries:
        # Молчание — часть контракта: сводка «сегодня бот ничего не выключил»
        # это шум, из-за которого перестают читать и настоящие сводки.
        logger.info("autonomous_pause: за день автономных пауз не было — сводку не шлём")
        return False
    from services.autopilot import _build_pause_undo_buttons
    from services.telegram_bot import send_with_buttons

    buttons = _build_pause_undo_buttons(
        [
            {"id": str(item.get("ad_id") or ""), "name": str(item.get("name") or "")}
            for item in entries
            if item.get("ad_id")
        ]
    )
    return bool(send_with_buttons(format_daily_summary(entries), buttons))
