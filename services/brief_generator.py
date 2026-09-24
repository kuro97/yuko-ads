"""
Сценарист v2: темы → сценарии → двойной гейт проверок → одобрение владельца в
Telegram → Trello «ТЗ реклам» (ARCH-brief-approval-flow.md).

Цикл (см. ARCH-phase3-scenarist.md §T7, ARCH-brief-approval-flow.md):
1. topic_selector.select_topics — темы из пробелов покрытия (приоритет) +
   teardown победителей по факту продаж (остаток до max_briefs)
2. _generate_scenario_v2 (LLM, Claude Sonnet, build_scenario_prompt) → ОДИН
   цельный сценарий на тему, логируется через llm_logger (purpose=scenario_generate)
3. scenario_validator.validate_scenario — детерминированные блок-проверки
   (пакет↔продукт, числа без источника, смешение голосов, мисматч формата,
   выдуманный дедлайн). Провал → карточка НЕ создаётся.
4. scenario_validator.self_critique — второй LLM-вызов (fail-closed).
   verdict="BLOCK" → карточка НЕ создаётся.
5. _classify_topic_product — продукт карточки (PRODA/PRODB/СТАРТ/ОБЩАЯ,
   ARCH-product-tags.md): target_product победителя-референса (teardown-темы) →
   keyword-эвристика (название + угол + сценарий) → LLM-добор (та же
   двухступенчатая схема, что и в бэкфилле creative_intelligence.backfill_target_product).
6. Оба гейта пройдены → ТЗ кладётся в файловую очередь pending_briefs
   (status=pending) и уходит владельцу в Telegram превью с кнопками
   «✅ Одобрить»/«❌ Отклонить». Карточка в Trello создаётся ТОЛЬКО по клику
   «Одобрить» (services/telegram_bot.py::_execute_approve_brief) — сразу в
   колонку «ТЗ реклам» (BRIEF_LIST_NAME). Никакого прямого create_card здесь.
7. Telegram-уведомление о блоках (заблокированные темы) — как раньше.

Только подготовка ТЗ (очередь + превью) — НИКАКИХ FB-действий и никакого
прямого создания карточек Trello на этапе генерации.
Если LLM недоступен, вернул пустой сценарий, или проверки заблокировали —
ТЗ НЕ уходит в очередь (лучше 0 ТЗ, чем плохой/непроверенный сценарий).
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Naive-метки прогонов (datetime.now().isoformat() ниже) — локальное время
# сервера, которое отчёты считают локальным временем бизнеса (UTC+5 по
# умолчанию — поменяйте смещение под свою зону; см. morning_digest).
_TZ_LOCAL = timezone(timedelta(hours=5))

# Ленивые импорты внешних зависимостей вынесены в функции (из-за circular imports),
# но notifications/pending_briefs/telegram_bot безопасны для верхнего уровня —
# pending_briefs только stdlib+threading, telegram_bot на верхнем уровне тянет
# только stdlib (json/logging/os/re/tempfile/pathlib), проектные зависимости —
# лениво внутри функций (циклов импорта нет).
from services.notifications import send_telegram  # noqa: E402  (остаётся — для блок-алертов)
from services.pending_briefs import add_pending, active_signatures, expire_old  # noqa: E402
from services.telegram_bot import send_with_buttons  # noqa: E402

# Файл состояния — хранит сигнатуры уже созданных ТЗ (чтобы не дублировать) и
# метки последнего запуска. last_scheduled_run_date/last_manual_run_date
# разделены: раньше был единый last_run_date, который писали
# И крон, И ручные запуски (/brief в telegram_console, HTTP-эндпоинт
# /api/autopilot/generate-briefs-now) — из-за этого ручной прогон накануне мог
# случайно "съесть" плановый слот крона (should_run_generator блокировал
# плановый пн/чт прогон интервалом MIN_INTERVAL_DAYS, хотя крон реально ни разу
# не запускался). Теперь интервальный гейт смотрит ТОЛЬКО на
# last_scheduled_run_date — эту метку пишет исключительно крон
# (_cron_brief_generator в web/app.py, а также generate_and_push_briefs при
# trigger="scheduled"). last_manual_run_date — чисто информационная метка
# ручных запусков, гейт её не читает.
STATE_FILE = Path(__file__).parent.parent / "data" / "brief_gen_state.json"

# Минимальный интервал между запусками (в днях) — LLM-вызовы платные.
# Каденция x2 в неделю (в сезон PRODA и общие темы льём активнее).
# Крон стоит на пн+чт (_cron_brief_generator в web/app.py) —
# пн→чт = 3 дня, значение 2 пропускает четверговый слот и держит страховку
# от повторного срабатывания в тот же/соседний день (интервал 1 день не спас
# бы от дубля при ретрае крона в пределах суток).
MIN_INTERVAL_DAYS = 2

# Сколько тем запрашивать у topic_selector.select_topics за один прогон.
# Раньше запрашивали ровно max_briefs
# тем — если верхние по рангу оказывались дублями (уже в state/активной
# очереди), прогон уходил созданиями=0, хотя select_topics строит ПОЛНЫЙ
# ранжированный список ВНУТРИ себя до среза [:max_topics] (topic_selector.py
# select_topics/_prioritize) — ниже по рангу могли быть ещё непросмотренные
# темы. Теперь запрашиваем заведомо избыточный пул — сам select_topics не
# делает лишней работы от большего max_topics (coverage-темы ограничены
# реальными пробелами по городам, teardown — жёстко max_n=10 в
# _teardown_topics, единственная разница — размер среза в конце), а
# generate_and_push_briefs при дубле берёт NEXT тему по рангу из этого пула.
_TOPIC_POOL_SIZE = 50


def _load_state() -> dict:
    """Загружает состояние генератора из файла.

    Миграция старого state-файла: раньше был единый ключ
    last_run_date для интервального гейта. Если в файле есть last_run_date, но
    нет нового last_scheduled_run_date — трактуем старое значение как
    last_scheduled_run_date, чтобы существующий state-файл не потерял историю
    и гейт продолжил работать без падения.
    """
    try:
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text())
            if "last_scheduled_run_date" not in state and state.get("last_run_date"):
                state["last_scheduled_run_date"] = state["last_run_date"]
            state.setdefault("last_scheduled_run_date", None)
            state.setdefault("last_manual_run_date", None)
            state.setdefault("generated_signatures", [])
            return state
    except Exception as exc:
        logger.warning("brief_generator: не удалось прочитать state — %s", exc)
    return {"last_scheduled_run_date": None, "last_manual_run_date": None, "generated_signatures": []}


def _save_state(state: dict) -> None:
    """Сохраняет состояние генератора."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as exc:
        logger.warning("brief_generator: не удалось сохранить state — %s", exc)


def _make_brief_signature(topic: dict) -> str:
    """Уникальная сигнатура темы — угол + город + формат. Определяет дубли.

    Формат добавлен в сигнатуру (v2, ARCH-phase3-scenarist §T7): один и тот же
    угол/город может дать разные ТЗ под разные форматы (video_speaker vs carousel),
    это не дубль, а два разных сценария.
    """
    return f"{topic.get('angle', '')}::{topic.get('city', '')}::{topic.get('ad_format', '')}"


def should_run_generator(now: datetime, state: dict) -> bool:
    """Проверяет гейт частоты — не чаще MIN_INTERVAL_DAYS.

    Смотрит ТОЛЬКО на last_scheduled_run_date — эту метку
    пишет исключительно плановый крон, ручные запуски (/brief, HTTP-эндпоинт
    generate-briefs-now) её не трогают и не могут украсть плановый слот.
    Обратная совместимость: если last_scheduled_run_date в state нет (старый
    state-файл до миграции или state собран вручную в тестах), используем
    legacy last_run_date.

    Args:
        now: текущее время (локальное)
        state: словарь состояния

    Returns:
        True если можно запускать
    """
    last_run = state.get("last_scheduled_run_date") or state.get("last_run_date")
    if not last_run:
        return True
    try:
        last_dt = datetime.fromisoformat(last_run).date()
        delta = (now.date() - last_dt).days
        return delta >= MIN_INTERVAL_DAYS
    except (ValueError, TypeError):
        return True


def _latest_brief_run_date(state: dict) -> str | None:
    """Возвращает метку самого свежего запуска генератора (для отчётов).

    Закрывает регрессию разделения меток (не путать с гейтом
    should_run_generator выше, который намеренно смотрит ТОЛЬКО на
    last_scheduled_run_date — ручные прогоны не должны красть плановый слот):
    services/evening_report.py (секция «Карточки ТЗ») и
    services/morning_digest.py (секция «сделал за сутки») читают тот же
    state-файл напрямую (без should_run_generator) и ДО этого фикса смотрели
    на единый last_run_date — после разделения на
    last_scheduled_run_date/last_manual_run_date он перестал писаться, и обе
    секции молча переставали видеть прогоны (плановые И ручные). Здесь —
    другая семантика: отчётам важен факт ЛЮБОГО прогона (плановый или
    ручной), поэтому берём более свежую из двух меток.

    Args:
        state: dict из brief_gen_state.json (сырое чтение json.loads, не
            обязательно через _load_state — вызывающие модули читают файл
            напрямую)

    Returns:
        ISO-строка даты/времени самого свежего запуска, или None, если
        запусков не было вообще (ни новых ключей, ни legacy last_run_date).
    """
    candidates = [ts for ts in (state.get("last_scheduled_run_date"), state.get("last_manual_run_date")) if ts]
    if not candidates:
        # Legacy fallback — старый state/кэш до разделения меток, где
        # был только единый last_run_date.
        return state.get("last_run_date")
    if len(candidates) == 1:
        return candidates[0]

    def _parse_key(ts: str) -> datetime:
        # Обе стороны сравнения приводим к aware UTC. Метки пишут разные
        # авторы: этот модуль — naive datetime.now().isoformat(), крон в
        # web/app.py (_cron_brief_generator) — aware now.isoformat(). Прямой
        # max() naive и aware падал с «can't compare offset-naive and
        # offset-aware datetimes», и отчёты молча теряли счётчик карточек ТЗ
        # («карточки ТЗ за 24ч недоступны» в morning_digest).
        try:
            parsed = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            # Битый формат одной из меток не должен ронять сравнение —
            # просто не даём ей выиграть у валидной.
            return datetime.min.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_TZ_LOCAL)
        return parsed.astimezone(timezone.utc)

    return max(candidates, key=_parse_key)


def _get_fresh_ads(days: int = 14) -> list[dict]:
    """Получает свежие рекламные данные из локальной БД (creative_kb).

    ВАЖНО: раньше читал data/analytics_cache.json, где qual_pct/payments часто
    None (не 0, а вообще отсутствуют) — ненадёжный источник. creative_kb в
    data/decisions.db — тот же источник, что уже использует автопилот пауз
    (services/shadow_report.py::_fetch_ads_from_local_db), там qual_pct/payments
    надёжные (привязка по fb_ad_id, свежий расход).

    Собственный SQL-запрос (не переиспользуем _fetch_ads_from_local_db
    напрямую), т.к. та функция уже используется в проде автопилотом и её
    формат заточен под score_and_decide — здесь нужны свои поля
    (angle-анализ requires: name/ad_name, spend, leads, cpl, hook_rate,
    hold_rate, frequency, days_running, qual_pct, payments).

    Не делает запросы к FB — читает локальную SQLite синхронизируемую кроном.
    При отсутствии БД/данных — возвращает пустой список (не бросает исключений).

    Args:
        days: период в днях — не используется для фильтрации в SQL (creative_kb
            хранит только актуальный снэпшот, а не историю по дням), оставлен
            в сигнатуре для обратной совместимости вызывающего кода.

    Returns:
        Список рекламных объявлений с метриками (name, spend, leads, cpl,
        hook_rate, hold_rate, frequency, days_running, qual_pct, payments,
        target_product — продукт из creative_kb, ARCH-product-tags.md; None,
        если колонки нет в схеме — деградация без падения)
    """
    from services.creative_intelligence import DB_PATH

    if DB_PATH is None:
        logger.warning("brief_generator: DB_PATH не инициализирован (creative_kb)")
        return []

    # target_product (ARCH-product-tags.md) — продукт победителя из creative_kb,
    # чтобы teardown-темы Сценариста v2 знали продукт без keyword-гадания
    # (см. select_winner_teardowns). Колонки может не быть на старой/тестовой
    # схеме (минимальная таблица в тестах) — тогда откатываемся на запрос без
    # неё, target_product уйдёт как None (дальше classify_product сам решит по
    # keyword/LLM, деградация не критична).
    _BASE_COLUMNS = (
        "ad_name, spend, leads, cpl, hook_rate, hold_rate, "
        "frequency, days_running, qual_pct, payments"
    )
    _QUERY_WITH_PRODUCT = (
        f"SELECT {_BASE_COLUMNS}, target_product FROM creative_kb "
        "WHERE effective_status = 'ACTIVE' AND spend > 0"
    )
    _QUERY_WITHOUT_PRODUCT = (
        f"SELECT {_BASE_COLUMNS} FROM creative_kb "
        "WHERE effective_status = 'ACTIVE' AND spend > 0"
    )

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            try:
                rows = conn.execute(_QUERY_WITH_PRODUCT).fetchall()
            except sqlite3.OperationalError:
                rows = conn.execute(_QUERY_WITHOUT_PRODUCT).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("brief_generator: не удалось прочитать creative_kb — %s", exc)
        return []

    ads: list[dict] = []
    for row in rows:
        spend = float(row["spend"] or 0)
        leads = int(row["leads"] or 0)
        # cpl в creative_kb может быть 0/неактуальным на старых записях —
        # считаем на лету если есть лиды, это надёжнее хранимого значения
        cpl = float(row["cpl"] or 0) if row["cpl"] else (spend / leads if leads else 0)
        row_keys = row.keys()
        ads.append({
            "name": row["ad_name"] or "",
            "spend": spend,
            "leads": leads,
            "cpl": cpl,
            "hook_rate": row["hook_rate"] if row["hook_rate"] is not None else 0,
            "hold_rate": row["hold_rate"] if row["hold_rate"] is not None else 0,
            "frequency": row["frequency"] if row["frequency"] is not None else 0,
            "days_running": int(row["days_running"] or 0),
            # qual_pct/payments — намеренно None если нет данных (не 0!),
            # analyze_winners различает "нет данных" от "0 качества/оплат"
            "qual_pct": row["qual_pct"],
            "payments": row["payments"],
            "target_product": row["target_product"] if "target_product" in row_keys else None,
        })

    return ads


def _generate_scenario_for_brief(brief: dict) -> str:
    """Генерирует ОДИН цельный сценарий через LLM (Claude Sonnet).

    Прямой вызов Anthropic API (как в services/hypotheses.py) — не через
    agent.copywriter.generate_ad_texts, тот заточен под 3-10 коротких
    нумерованных вариантов текста, а не под цельный живой сценарий.

    При отсутствии ANTHROPIC_API_KEY или ошибке LLM возвращает пустую
    строку — вызывающий код (generate_and_push_briefs) обязан пропустить
    создание карточки в этом случае, а не подставлять шаблон.

    Args:
        brief: словарь брифа из build_creative_briefs

    Returns:
        Текст сценария или пустая строка при недоступности/ошибке LLM
    """
    import config

    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        logger.warning("brief_generator: ANTHROPIC_API_KEY не задан — сценарий для '%s' пропущен",
                       brief.get("hypothesis", "?"))
        return ""

    try:
        import anthropic
        from services.creative_briefs import build_llm_prompt

        model = getattr(config, "CLAUDE_SONNET_MODEL", "claude-sonnet-5")
        client = anthropic.Anthropic(api_key=api_key)
        prompt = build_llm_prompt(brief)

        response = client.messages.create(
            model=model,
            max_tokens=2048,
            # Задача чисто творческая (написать рекламный сценарий) — reasoning
            # не нужен, adaptive thinking только тратит токены впустую и меняет
            # порядок блоков в response.content (см. извлечение текста ниже).
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        # Кончились кредиты Anthropic → алерт (дедуп) + прерываем батч-цикл сценариев
        from services.llm_credit_guard import raise_if_credit_error
        raise_if_credit_error("brief_generator._generate_scenario_for_brief", exc)
        logger.warning("brief_generator: LLM сценарий не удался для '%s' — %s",
                       brief.get("hypothesis", "?"), exc)
        return ""

    if not response.content:
        logger.warning("brief_generator: LLM вернул пустой ответ для '%s'",
                       brief.get("hypothesis", "?"))
        return ""

    # Не полагаемся на content[0] — при включённом thinking первым блоком
    # идёт ThinkingBlock без атрибута .text. Ищем первый текстовый блок явно,
    # это защищает и от будущих новых типов блоков от Anthropic.
    text_block = next((block for block in response.content if getattr(block, "type", None) == "text"), None)
    if text_block is None:
        logger.warning("brief_generator: LLM ответ не содержит текстового блока для '%s'",
                       brief.get("hypothesis", "?"))
        return ""

    scenario = text_block.text.strip()
    if not scenario:
        logger.warning("brief_generator: LLM вернул пустой сценарий для '%s'",
                       brief.get("hypothesis", "?"))
        return ""

    return scenario


def _generate_scenario_v2(topic: dict, fact_sheet: dict) -> str:
    """Генерирует ОДИН цельный сценарий через LLM под тему Сценариста v2.

    Промпт строится scenario_prompt.build_scenario_prompt (факты + формат +
    референс + guardrails). Вызов Anthropic — тот же рабочий паттерн, что и
    в _generate_scenario_for_brief (thinking disabled + явное извлечение
    text-блока, т.к. при включённом thinking первым блоком идёт ThinkingBlock
    без атрибута .text). Дополнительно логируется через llm_logger
    (purpose="scenario_generate", см. ARCH-phase3-scenarist.md §T7/AC9).

    При отсутствии ANTHROPIC_API_KEY или ошибке LLM возвращает пустую
    строку — вызывающий код обязан пропустить создание карточки.

    Args:
        topic: тема из topic_selector.select_topics()
        fact_sheet: dict из fact_sheet.load_fact_sheet()

    Returns:
        Текст сценария или пустая строка при недоступности/ошибке LLM
    """
    import config
    from services.scenario_prompt import build_scenario_prompt

    api_key = config.ANTHROPIC_API_KEY
    if not api_key:
        logger.warning("brief_generator: ANTHROPIC_API_KEY не задан — сценарий для темы '%s' пропущен",
                       topic.get("angle", "?"))
        return ""

    model = getattr(config, "CLAUDE_SONNET_MODEL", "claude-sonnet-5")
    prompt = build_scenario_prompt(topic, fact_sheet)

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        start = time.monotonic()
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            # Задача чисто творческая (написать рекламный сценарий) — reasoning
            # не нужен, adaptive thinking только тратит токены впустую и меняет
            # порядок блоков в response.content (см. извлечение текста ниже).
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = int((time.monotonic() - start) * 1000)
    except Exception as exc:
        # Кончились кредиты Anthropic → алерт (дедуп) + прерываем батч-цикл сценариев
        from services.llm_credit_guard import raise_if_credit_error
        raise_if_credit_error("brief_generator._generate_scenario_v2", exc)
        logger.warning("brief_generator: LLM сценарий не удался для темы '%s' — %s",
                       topic.get("angle", "?"), exc)
        return ""

    # Логируем LLM-вызов (AC9). Некритично — не должно ронять генерацию.
    try:
        from services.llm_logger import log_llm_call

        usage = getattr(response, "usage", None)
        log_llm_call(
            model=model,
            purpose="scenario_generate",
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            latency_ms=latency_ms,
        )
    except Exception as exc:
        logger.warning("brief_generator: не удалось залогировать LLM-вызов — %s", exc)

    if not response.content:
        logger.warning("brief_generator: LLM вернул пустой ответ для темы '%s'",
                       topic.get("angle", "?"))
        return ""

    # Не полагаемся на content[0] — при включённом thinking первым блоком
    # идёт ThinkingBlock без атрибута .text. Ищем первый текстовый блок явно,
    # это защищает и от будущих новых типов блоков от Anthropic.
    text_block = next((block for block in response.content if getattr(block, "type", None) == "text"), None)
    if text_block is None:
        logger.warning("brief_generator: LLM ответ не содержит текстового блока для темы '%s'",
                       topic.get("angle", "?"))
        return ""

    scenario = text_block.text.strip()
    if not scenario:
        logger.warning("brief_generator: LLM вернул пустой сценарий для темы '%s'",
                       topic.get("angle", "?"))
        return ""

    return scenario


def _format_topic_card(topic: dict, scenario: str, critique_reason: str) -> dict:
    """Формирует карточку Trello: сценарий целиком + короткий служебный блок.

    Тело карточки = чистый сценарий + служебный блок (референс/формат/режим/
    метки проверок). Никаких бюрократических полей (Аудитория/Тон/Эмоция/
    Приоритет запрещены — AC8).

    Args:
        topic: тема из topic_selector.select_topics()
        scenario: непустой текст сценария, прошедший оба гейта
        critique_reason: причина PASS от self_critique (для служебного блока)

    Returns:
        {"name": ..., "desc": ...} для create_card
    """
    ad_format = topic.get("ad_format", "?")
    voice = topic.get("voice", "brand")
    angle = topic.get("angle", "?")
    city = topic.get("city", "")
    reference = topic.get("reference")

    service_lines = [
        f"*Сценарист v2 · формат: {ad_format} · голос: {voice}*",
        f"*Проверки: validate_scenario ✅ · self_critique ✅ ({critique_reason})*",
    ]
    if reference:
        service_lines.append(
            f"*Референс: {reference.get('name', '?')} — CPL ${reference.get('cpl', '?')}, "
            f"{reference.get('leads', '?')} лидов, {reference.get('payments', '?')} оплат*"
        )

    body = f"{scenario.strip()}\n\n---\n" + "\n".join(service_lines)

    name = f"{city} / {angle}" if city else angle

    return {"name": name, "desc": body}


def _classify_topic_product(topic: dict, card_name: str, scenario: str) -> str:
    """Определяет продукт карточки ТЗ для Trello-метки (ARCH-product-tags.md).

    Тот же приоритет источников, что и в двухступенчатом бэкфилле
    (services.creative_intelligence.backfill_target_product):
      1) target_product победителя-референса — только у teardown-тем есть
         topic["reference"] (см. creative_briefs.select_winner_teardowns);
         у coverage-тем reference=None, значит источника нет.
      2) keyword-эвристика по названию карточки + углу + сценарию
         (services.product_tags.classify_product).
      3) LLM-добор (product_tags._llm_classify_product) — ТОЛЬКО если keyword
         не поймал (результат ОБЩАЯ). Ловит эмоциональные PRODA-заходы без
         явных ключевых слов («Критика рынка», «История Олега» и т.п., см. §5
         спеки) — та же проблема, что решает бэкфилл, но для НОВЫХ карточек.

    Fail-safe: classify_product и _llm_classify_product сами не бросают
    исключений (дефолт ОБЩАЯ) — эта функция дополнительно оборачивает вызов
    в try/except, чтобы сбой классификации НИКОГДА не блокировал создание
    карточки (лучше карточка с ОБЩАЯ, чем отсутствие карточки).

    Args:
        topic: тема из topic_selector.select_topics()
        card_name: итоговое название карточки (_format_topic_card()["name"])
        scenario: текст сценария, прошедший оба гейта

    Returns:
        Канон продукта из product_tags.VALID_PRODUCTS
    """
    from services.product_tags import DEFAULT_PRODUCT, _llm_classify_product, classify_product
    from services.llm_credit_guard import CreditBalanceError

    try:
        reference = topic.get("reference") or {}
        target_product = reference.get("target_product")
        angle = topic.get("angle", "")

        product = classify_product(name=card_name, desc=f"{angle} {scenario}", target_product=target_product)
        if product == DEFAULT_PRODUCT:
            product = _llm_classify_product(card_name, scenario)
        return product
    except CreditBalanceError:
        # Кредиты Anthropic кончились — пробрасываем, чтобы батч-цикл прервался
        raise
    except Exception as exc:
        logger.warning("brief_generator: классификация продукта для '%s' упала, дефолт ОБЩАЯ — %s",
                       card_name, exc)
        return DEFAULT_PRODUCT


def _format_brief_preview(name: str, product: str | None, desc: str) -> str:
    """Читабельное HTML-превью ТЗ для Telegram (владелец жёстко требует читабельность).

    Заголовок, метка продукта [PRODA]/[ОБЩАЯ]/…, суть сценария (без служебного
    блока карточки), пустые строки между блоками, человеческий язык. Экранирует
    пользовательский текст (html.escape) — сценарий может содержать <>&. Обрезает
    по безопасному лимиту (3500) с честной пометкой (полный текст будет в карточке).
    """
    import html

    # Тело карточки = "<сценарий>\n\n---\n<служебный блок>". В превью показываем
    # только сценарий; служебные *метки* проверок владельцу не нужны.
    scenario = desc.split("\n---\n", 1)[0].strip() if desc else ""

    product_tag = f"[{product}]" if product else "[ОБЩАЯ]"

    header = "📝 <b>Новое ТЗ — нужно твоё «ок»</b>"
    title = f"<b>{html.escape(str(name))}</b>"
    tag = f"🏷 {html.escape(product_tag)}"

    body = html.escape(scenario)
    _MAX_BODY = 3500
    if len(body) > _MAX_BODY:
        body = body[:_MAX_BODY].rstrip() + "\n…(полный текст будет в карточке после одобрения)"

    footer = "———\n✅ Одобрю — карточка уйдёт в «ТЗ реклам».\n❌ Отклоню — уберу из очереди."

    return f"{header}\n\n{title}\n{tag}\n\n{body}\n\n{footer}"


def generate_and_push_briefs(max_briefs: int = 3, trigger: str = "manual") -> dict:
    """Главная функция Сценариста v2: темы → сценарии → двойной гейт → очередь
    на одобрение владельца (ARCH-brief-approval-flow.md).

    1. topic_selector.select_topics — темы из пробелов покрытия + teardown победителей
       (запрашивается избыточный пул _TOPIC_POOL_SIZE, не ровно max_briefs —
       см. §3 ниже и комментарий у _TOPIC_POOL_SIZE)
    2. expire_old() — просроченные pending-записи очереди уходят в expired
    3. Фильтрация уже созданных (state-файл + active_signatures() очереди —
       сигнатура angle::city::ad_format)
       — при дубле среди верхних по рангу тем берём СЛЕДУЮЩУЮ тему из пула,
       пока не наберём max_briefs НЕ-дублей или пул не кончится. Раньше при
       дубле верхних тем прогон уходил с created=0, хотя пул содержал ещё
       непросмотренные темы ниже по рангу.
    4. Генерация сценария через LLM (_generate_scenario_v2)
    5. validate_scenario (детерминированные проверки) → провал = блок
    6. self_critique (LLM-самокритика, fail-closed) → BLOCK = блок
    7. Оба гейта пройдены → add_pending (status=pending) + send_with_buttons
       превью с кнопками «✅ Одобрить»/«❌ Отклонить». Карточка Trello НЕ
       создаётся здесь — только по клику владельца (telegram_bot).
    8. Telegram-уведомление о блоках (заблокированные темы)
    9. Обновление state-файла (только заблокированные сигнатуры — успешные
       темы дедупятся через очередь pending_briefs, не через state)

    Args:
        max_briefs: максимальное количество новых ТЗ за один запуск
        trigger: "scheduled" (плановый крон _cron_brief_generator, web/app.py)
            или "manual" (/brief в Telegram, HTTP /api/autopilot/generate-briefs-now
            — дефолт, реальные ручные вызывающие места). Управляет ТОЛЬКО тем,
            в какой ключ state пишется метка последнего запуска
            (last_scheduled_run_date vs last_manual_run_date) —
            интервальный гейт should_run_generator видит только scheduled-метку,
            ручной прогон никогда не блокирует плановый.

    Returns:
        {
            "queued": int,          # сколько ТЗ ушло владельцу на одобрение
            "created": int,         # алиас == queued (обратная совместимость лога крона)
            "skipped": int,         # дубли + пустой сценарий от LLM
            "blocked": int,         # заблокировано validate_scenario/self_critique
            "angles": list[str],    # углы отправленных на одобрение
            "pending": list[dict],  # [{"id", "name", "product"}, ...]
            "blocks": list[dict],   # [{"topic": str, "reason": str}, ...]
            "topics_seen": int,     # сколько тем из пула select_topics просмотрено
                                     # (дубли + добранные) — телеметрия дедупа
            "topics_skipped_dup": int,  # из них пропущено как дубль (angle::city::ad_format)
            "error": str | None,
        }
    """
    from services.fact_sheet import FactSheetError, load_fact_sheet
    from services.llm_credit_guard import CreditBalanceError
    from services.scenario_validator import self_critique, validate_scenario
    from services.topic_selector import select_topics

    result: dict = {
        "queued": 0,
        "created": 0,
        "skipped": 0,
        "blocked": 0,
        "angles": [],
        "pending": [],
        "blocks": [],
        "topics_seen": 0,
        "topics_skipped_dup": 0,
        "error": None,
    }

    # --- Fact Sheet: fail-closed — без него генерация ТЗ запрещена ---
    try:
        fact_sheet = load_fact_sheet()
    except FactSheetError as exc:
        result["error"] = f"Fact Sheet недоступен: {exc}"
        logger.error("brief_generator: Fact Sheet недоступен — %s", exc)
        return result

    # --- Выбор тем (пробелы покрытия > teardown победителей) ---
    # Запрашиваем избыточный пул (_TOPIC_POOL_SIZE), а не ровно max_briefs
    # (см. комментарий у _TOPIC_POOL_SIZE): если верхние по рангу темы дубли,
    # ниже по рангу могут быть НЕ-дубли.
    try:
        topics = select_topics(max_topics=_TOPIC_POOL_SIZE)
    except FactSheetError as exc:
        result["error"] = f"Fact Sheet недоступен: {exc}"
        logger.error("brief_generator: Fact Sheet недоступен при выборе тем — %s", exc)
        return result

    if not topics:
        result["error"] = "Нет тем для генерации (покрытие в норме, победителей нет)"
        logger.info("brief_generator: select_topics вернул пустой список")
        return result

    # --- Ретеншн очереди: просроченные pending -> expired (AC9) ---
    try:
        expire_old()
    except Exception as exc:
        logger.warning("brief_generator: expire_old упал — %s", type(exc).__name__)

    # --- Фильтрация дублей: state (заблокированные) + активная очередь (pending/approved) ---
    state = _load_state()
    # state_sigs — только то, что реально персистим в state.generated_signatures
    # (заблокированные темы). Отдельно от existing_sigs, чтобы активная
    # очередь (pending/approved) не утекала в state и не блокировала
    # тему навсегда после reject/expire (см. §6.5 п.4, п.7).
    state_sigs = set(state.get("generated_signatures", []))
    existing_sigs = set(state_sigs)
    try:
        existing_sigs |= active_signatures()
    except Exception as exc:
        logger.warning("brief_generator: active_signatures упал, дедуп только по state — %s",
                       type(exc).__name__)

    # Идём по пулу тем (уже отсортирован
    # по рангу select_topics) и добираем NEXT тему при дубле, пока не наберём
    # max_briefs НЕ-дублей или пул не кончится — раньше дубль среди верхних
    # тем "съедал" весь прогон (created=0), хотя ниже по рангу были свежие
    # темы. topics_seen/topics_skipped_dup — телеметрия для результата (не
    # влияют на логику, только отчётность).
    new_topics = []
    topics_seen = 0
    topics_skipped_dup = 0
    for topic in topics:
        topics_seen += 1
        sig = _make_brief_signature(topic)
        if sig in existing_sigs:
            topics_skipped_dup += 1
            result["skipped"] += 1
            logger.debug("brief_generator: пропускаем дубль темы '%s'", sig)
            continue
        new_topics.append(topic)
        if len(new_topics) >= max_briefs:
            break

    result["topics_seen"] = topics_seen
    result["topics_skipped_dup"] = topics_skipped_dup

    if not new_topics:
        # Честный результат "нечего" — весь пул select_topics оказался дублями
        # (уже созданы/в очереди), это НЕ ошибка выполнения (нет исключений,
        # LLM не звали) — result["error"] здесь чисто информационная строка
        # для человекочитаемых сводок (_format_brief_summary в
        # telegram_console.py показывает её текстом, не как алерт).
        logger.info(
            "brief_generator: весь пул тем — дубли (просмотрено=%d, дублей=%d), новых нет",
            topics_seen, topics_skipped_dup,
        )
        result["error"] = "Все темы уже созданы (дубли)"
        return result

    # --- Генерируем сценарии, прогоняем через двойной гейт, кладём в очередь ---
    created_angles = []
    new_sigs = []
    blocks: list[dict] = []

    for topic in new_topics:
        angle = topic.get("angle", "?")
        sig = _make_brief_signature(topic)
        try:
            # 1. Генерация. Если LLM недоступен/вернул пустоту — в очередь
            # класть НЕЛЬЗЯ (лучше 0 ТЗ, чем пустой сценарий).
            scenario_text = _generate_scenario_v2(topic, fact_sheet)
            if not scenario_text or not scenario_text.strip():
                result["skipped"] += 1
                logger.warning(
                    "brief_generator: пропускаем тему '%s' — LLM не вернул сценарий", angle,
                )
                continue

            # 2. Детерминированные блок-проверки.
            validation = validate_scenario(scenario_text, topic, fact_sheet)
            if not validation.passed:
                reason = "; ".join(validation.violations)
                result["blocked"] += 1
                blocks.append({"topic": angle, "reason": reason})
                logger.warning("brief_generator: тема '%s' заблокирована validate_scenario — %s",
                               angle, reason)
                new_sigs.append(sig)  # не пытаемся генерировать эту же тему повторно
                continue

            # 3. LLM-самокритика (fail-closed).
            critique = self_critique(scenario_text, topic)
            if critique.verdict != "PASS":
                result["blocked"] += 1
                blocks.append({"topic": angle, "reason": critique.reason})
                logger.warning("brief_generator: тема '%s' заблокирована self_critique — %s",
                               angle, critique.reason)
                new_sigs.append(sig)
                continue

            # 4. Оба гейта пройдены — кладём в очередь на одобрение владельца.
            card_data = _format_topic_card(topic, scenario_text, critique.reason)
            # 5. Продукт для Trello-метки (ARCH-product-tags.md) — вешается
            # при создании карточки ПОСЛЕ одобрения (telegram_bot._execute_approve_brief).
            product = _classify_topic_product(topic, card_data["name"], scenario_text)
            record = add_pending(name=card_data["name"], desc=card_data["desc"],
                                  product=product, signature=sig)

            preview = _format_brief_preview(card_data["name"], product, card_data["desc"])
            buttons = [[
                ("✅ Одобрить", f"approve_brief:{record['id']}"),
                ("❌ Отклонить", f"reject_brief:{record['id']}"),
            ]]
            # Некритично — запись уже в очереди, при недоступности Telegram
            # владелец увидит её позже через /brief или следующий прогон.
            try:
                send_with_buttons(preview, buttons)
            except Exception as exc:
                logger.warning("brief_generator: не удалось отправить превью ТЗ '%s' — %s",
                               angle, type(exc).__name__)

            result["pending"].append({"id": record["id"], "name": card_data["name"], "product": product})
            created_angles.append(angle)
            # sig НЕ пишем в new_sigs (state) — дедуп очереди делает
            # active_signatures(), отклонённые/просроченные темы смогут
            # вернуться позже.
            result["queued"] += 1
            result["created"] += 1
            logger.info("brief_generator: ТЗ '%s' отправлено на одобрение (id=%s)",
                        card_data["name"], record["id"])

        except CreditBalanceError:
            # Кредиты Anthropic кончились — алерт уже отправлен стражем, дальше молотить
            # бессмысленно (все следующие темы упадут так же). Прерываем цикл.
            logger.error("brief_generator: кредиты Anthropic кончились — прерываю генерацию ТЗ")
            break
        except Exception as exc:
            logger.error("brief_generator: ошибка обработки темы '%s' — %s", angle, exc)
            # Продолжаем — не блокируем остальные темы

    result["angles"] = created_angles
    result["blocks"] = blocks

    # --- Обновляем state (только заблокированные сигнатуры) ---
    # Ручной прогон не должен красть слот у планового крона: раньше сюда
    # писался единый last_run_date — его читал should_run_generator, поэтому
    # ручной прогон с хотя бы одним блоком (тот же путь, что и здесь) мог
    # "съесть" плановый слот крона. Теперь пишем в РАЗНЫЙ ключ по trigger —
    # last_scheduled_run_date (интервальный гейт читает только его) для
    # trigger="scheduled", last_manual_run_date (чисто информационно, гейт его
    # игнорирует) для остальных ("manual" и любое другое значение). Условие
    # (только при новых заблокированных сигнатурах) сохранено как было —
    # "пустой" прогон без блоков state не трогает.
    if new_sigs:
        all_sigs = list(state_sigs) + new_sigs
        # Храним не более 200 сигнатур (MAX 200 последних)
        state["generated_signatures"] = all_sigs[-200:]
        if trigger == "scheduled":
            state["last_scheduled_run_date"] = datetime.now().isoformat()
        else:
            state["last_manual_run_date"] = datetime.now().isoformat()
        _save_state(state)

    # --- Telegram-уведомление: блоки (заблокированные темы) ---
    for block in blocks:
        tg_text = f"🚫 <b>ТЗ заблокировано:</b> {block['reason']}\nТема: {block['topic']}"
        try:
            send_telegram(tg_text, channel="ads")
        except Exception as exc:
            logger.warning("brief_generator: не удалось отправить Telegram-алерт о блоке — %s", exc)

    logger.info(
        "brief_generator: готово — отправлено на одобрение %d, пропущено %d, заблокировано %d",
        result["queued"], result["skipped"], result["blocked"],
    )
    return result
