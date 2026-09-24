"""
Тесты services/brief_generator.py.

Сценарист v2 (ARCH-phase3-scenarist.md §T7): generate_and_push_briefs теперь
работает через topic_selector.select_topics -> _generate_scenario_v2 ->
validate_scenario -> self_critique -> очередь одобрения владельца
(ARCH-brief-approval-flow.md), а не через analyze_winners/build_creative_briefs
(v1, старый поток из "Идеи / ТЗ"). _generate_scenario_for_brief и связанные
тесты остаются без изменений — эта функция всё ещё используется отдельным
эндпоинтом /api/learning/v2 (web/app.py), вне рамок задачи T7.

Мокаем все внешние зависимости generate_and_push_briefs (v2):
- topic_selector.select_topics — источник тем
- fact_sheet.load_fact_sheet — Fact Sheet
- _generate_scenario_v2 — LLM генерация сценария
- scenario_validator.validate_scenario / self_critique — двойной гейт
- send_with_buttons — превью ТЗ в Telegram (карточка Trello теперь создаётся
  только по клику "Одобрить" в telegram_bot._execute_approve_brief, ЗДЕСЬ НЕ
  тестируется — см. tests/test_telegram_brief_approval.py)
- send_telegram — блок-алерты

State-файл и очередь pending_briefs переопределяем через monkeypatch на tmp_path.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта
import sys as _sys
if "google.genai" not in _sys.modules:
    _sys.modules["google.genai"] = MagicMock()
if "google" not in _sys.modules:
    _sys.modules["google"] = MagicMock()


# ---------------------------------------------------------------------------
# Вспомогательные данные
# ---------------------------------------------------------------------------

_TZ_LOCAL = timezone(timedelta(hours=5))


def _make_brief(angle: str = "Страх ошибки", city: str = "CityA") -> dict:
    """Создаёт тестовый бриф."""
    return {
        "type": "expand",
        "priority": "HIGH",
        "hypothesis": f"Запустить '{angle}' в {city}",
        "rationale": "CPL $4, 10 лидов. Тема работает.",
        "audience": f"Потенциальные клиенты в {city}",
        "message": "Тестовое сообщение",
        "hook_direction": "Тестовый хук",
        "angle": angle,
        "emotion": "Страх → облегчение",
        "tone": "Авторитетный",
        "format": "Reels 9:16",
        "references": [{"name": "Тест | Страх ошибки", "cpl": 3.5, "leads": 8}],
        "city": city,
        "topic": angle,
    }


def _make_ads() -> list[dict]:
    """Создаёт тестовые рекламные объявления с CPL < 10 чтобы build_creative_briefs
    вернул брифы (условие: avg_cpl <= 10, leads >= 2)."""
    return [
        {
            "name": "CityA | Страх ошибки / V1",
            "spend": 50.0,
            "leads": 15,
            "cpl": 3.3,
            "hook_rate": 0.4,
            "hold_rate": 0.2,
            "frequency": 1.8,
            "days_running": 10,
        },
        {
            "name": "CityB | Результат / V2",
            "spend": 40.0,
            "leads": 8,
            "cpl": 5.0,
            "hook_rate": 0.3,
            "hold_rate": 0.15,
            "frequency": 1.2,
            "days_running": 7,
        },
        {
            "name": "CityC | PRODA запуск / V1",
            "spend": 30.0,
            "leads": 3,
            "cpl": 10.0,
            "hook_rate": 0.2,
            "hold_rate": 0.1,
            "frequency": 1.0,
            "days_running": 5,
        },
    ]


# ---------------------------------------------------------------------------
# Тесты should_run_generator (гейт частоты)
#
# Каденция x2 в неделю: крон стоит на
# понедельник+четверг (_cron_brief_generator в web/app.py), MIN_INTERVAL_DAYS
# снижен 5 -> 2 (страховка от дублей в тот же/соседний день, но не блокирует
# четверговый слот после понедельника — пн->чт = 3 дня).
# ---------------------------------------------------------------------------

def test_should_run_generator_first_time():
    """Первый запуск — нет last_run_date, можно запускать."""
    from services.brief_generator import should_run_generator

    now = datetime.now(_TZ_LOCAL)
    state = {"last_run_date": None, "generated_signatures": []}
    assert should_run_generator(now, state) is True


def test_should_run_generator_too_soon():
    """Последний запуск был вчера — слишком рано."""
    from services.brief_generator import should_run_generator, MIN_INTERVAL_DAYS

    now = datetime.now(_TZ_LOCAL)
    recent = (now - timedelta(days=MIN_INTERVAL_DAYS - 1)).isoformat()
    state = {"last_run_date": recent, "generated_signatures": []}
    assert should_run_generator(now, state) is False


def test_should_run_generator_after_interval():
    """Прошло достаточно дней — можно запускать."""
    from services.brief_generator import should_run_generator, MIN_INTERVAL_DAYS

    now = datetime.now(_TZ_LOCAL)
    old = (now - timedelta(days=MIN_INTERVAL_DAYS + 1)).isoformat()
    state = {"last_run_date": old, "generated_signatures": []}
    assert should_run_generator(now, state) is True


def test_should_run_generator_monday_to_thursday_3_days_passes():
    """Пн -> чт (3 дня) — новый рабочий слот каденции x2, гейт должен пропускать."""
    from services.brief_generator import should_run_generator

    monday = datetime(2026, 6, 29, 8, 15, tzinfo=_TZ_LOCAL)
    thursday = datetime(2026, 7, 2, 8, 15, tzinfo=_TZ_LOCAL)
    state = {"last_run_date": monday.isoformat(), "generated_signatures": []}
    assert should_run_generator(thursday, state) is True


def test_should_run_generator_monday_to_tuesday_1_day_blocked():
    """Пн -> вт (1 день) — соседний день, страховка от дубля должна блокировать."""
    from services.brief_generator import should_run_generator

    monday = datetime(2026, 6, 29, 8, 15, tzinfo=_TZ_LOCAL)
    tuesday = datetime(2026, 6, 30, 8, 15, tzinfo=_TZ_LOCAL)
    state = {"last_run_date": monday.isoformat(), "generated_signatures": []}
    assert should_run_generator(tuesday, state) is False


# ---------------------------------------------------------------------------
# Фикс 2026-07-09: ручной прогон (/brief, HTTP-эндпоинт generate-briefs-now)
# не должен красть слот у планового крона. should_run_generator теперь смотрит
# ТОЛЬКО на last_scheduled_run_date — last_manual_run_date гейт игнорирует.
# (тесты выше на legacy-ключе last_run_date остаются зелёными за счёт fallback
# в should_run_generator — это и есть обратная совместимость со старым state).
# ---------------------------------------------------------------------------

def test_should_run_generator_manual_run_yesterday_does_not_block_scheduled():
    """Ручной прогон вчера (last_manual_run_date) НЕ блокирует плановый сегодня —
    гейт читает только last_scheduled_run_date, которого в state ещё нет."""
    from services.brief_generator import should_run_generator

    now = datetime.now(_TZ_LOCAL)
    yesterday = (now - timedelta(days=1)).isoformat()
    state = {"last_scheduled_run_date": None, "last_manual_run_date": yesterday,
              "generated_signatures": []}
    assert should_run_generator(now, state) is True


def test_should_run_generator_scheduled_run_yesterday_blocks_scheduled_today():
    """Плановый прогон вчера (last_scheduled_run_date) блокирует плановый
    сегодня — интервал MIN_INTERVAL_DAYS (2 дня) ещё не прошёл."""
    from services.brief_generator import should_run_generator

    now = datetime.now(_TZ_LOCAL)
    yesterday = (now - timedelta(days=1)).isoformat()
    state = {"last_scheduled_run_date": yesterday, "last_manual_run_date": None,
              "generated_signatures": []}
    assert should_run_generator(now, state) is False


def test_manual_run_yesterday_does_not_block_scheduled_run_today_via_state_file(tmp_path, monkeypatch):
    """Регрессия бага 2026-07-09: сквозной прогон через файл состояния —
    ручной прогон вчера (_save_state с last_manual_run_date) не мешает
    should_run_generator пропустить плановый прогон сегодня."""
    import services.brief_generator as bg_module
    state_path = tmp_path / "brief_gen_state.json"
    monkeypatch.setattr(bg_module, "STATE_FILE", state_path)

    now = datetime.now(_TZ_LOCAL)
    yesterday = now - timedelta(days=1)
    bg_module._save_state({
        "last_scheduled_run_date": None,
        "last_manual_run_date": yesterday.isoformat(),
        "generated_signatures": [],
    })

    loaded = bg_module._load_state()
    assert bg_module.should_run_generator(now, loaded) is True


# ---------------------------------------------------------------------------
# Тесты _get_fresh_ads — источник данных creative_kb (data/decisions.db),
# НЕ analytics_cache.json (там qual_pct/payments часто None — ненадёжно)
# ---------------------------------------------------------------------------

def _init_test_creative_kb(db_path):
    """Создаёт минимальную схему creative_kb и наполняет тестовыми данными
    (только колонки, которые реально селектит _get_fresh_ads)."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE creative_kb (
            ad_id TEXT PRIMARY KEY,
            ad_name TEXT,
            effective_status TEXT,
            spend REAL,
            leads INTEGER,
            cpl REAL,
            hook_rate REAL,
            hold_rate REAL,
            frequency REAL,
            days_running INTEGER,
            qual_pct REAL,
            payments INTEGER
        )
        """
    )
    conn.execute(
        """
        INSERT INTO creative_kb
        (ad_id, ad_name, effective_status, spend, leads, cpl, hook_rate,
         hold_rate, frequency, days_running, qual_pct, payments)
        VALUES
        ('ad1', 'CityB | Карусель / Пример', 'ACTIVE',
         500.0, 100, 5.0, 0.4, 0.2, 1.8, 20, 0.0, 0),
        ('ad2', 'CityA | Страх ошибки / V1', 'ACTIVE',
         50.0, 15, 3.3, 0.4, 0.2, 1.8, 10, 25.0, 3),
        ('ad3', 'CityC | Результат / V1 (пауза)', 'PAUSED',
         30.0, 5, 6.0, 0.2, 0.1, 1.0, 5, NULL, NULL)
        """
    )
    conn.commit()
    conn.close()


def test_get_fresh_ads_reads_from_creative_kb(tmp_path, monkeypatch):
    """_get_fresh_ads читает из creative_kb (SQLite), а не из analytics_cache.json."""
    import services.creative_intelligence as ci_module
    from services.brief_generator import _get_fresh_ads

    db_path = tmp_path / "decisions.db"
    _init_test_creative_kb(db_path)
    monkeypatch.setattr(ci_module, "DB_PATH", str(db_path))

    ads = _get_fresh_ads()

    names = [a["name"] for a in ads]
    # Только ACTIVE со spend>0 — паузнутое ad3 не попадает
    assert "CityB | Карусель / Пример" in names
    assert "CityA | Страх ошибки / V1" in names
    assert "CityC | Результат / V1 (пауза)" not in names


def test_get_fresh_ads_preserves_qual_pct_and_payments_as_none_when_missing(tmp_path, monkeypatch):
    """qual_pct/payments сохраняются как реальные значения из creative_kb
    (не подменяются на 0 когда None) — это и есть исправление бага с
    ненадёжным analytics_cache.json."""
    import services.creative_intelligence as ci_module
    from services.brief_generator import _get_fresh_ads

    db_path = tmp_path / "decisions.db"
    _init_test_creative_kb(db_path)
    monkeypatch.setattr(ci_module, "DB_PATH", str(db_path))

    ads = _get_fresh_ads()
    carousel_ad = next(a for a in ads if "Карусель" in a["name"])

    assert carousel_ad["qual_pct"] == 0.0
    assert carousel_ad["payments"] == 0


def test_get_fresh_ads_no_db_path_returns_empty(monkeypatch):
    """DB_PATH не инициализирован (None) — возвращает пустой список, не падает."""
    import services.creative_intelligence as ci_module
    from services.brief_generator import _get_fresh_ads

    monkeypatch.setattr(ci_module, "DB_PATH", None)

    assert _get_fresh_ads() == []


# ---------------------------------------------------------------------------
# Тесты _make_brief_signature (v2: теперь принимает topic, сигнатура включает
# ad_format — угол+город могут повторяться под разные форматы, это не дубль)
# ---------------------------------------------------------------------------

def _make_topic(angle: str = "Страх ошибки", city: str = "CityA", ad_format: str = "video_speaker") -> dict:
    """Создаёт тестовую тему (топик) v2 — минимальный набор полей из §6.1 спеки."""
    return {
        "source": "coverage",
        "city": city,
        "segment": "общий",
        "voice": "brand",
        "ad_format": ad_format,
        "reference": None,
        "variable_to_vary": "город",
        "angle": angle,
        "priority": "HIGH",
        "rationale": "тест",
    }


def test_make_brief_signature_unique():
    """Сигнатура зависит от угла, города и формата."""
    from services.brief_generator import _make_brief_signature

    t1 = _make_topic(angle="Страх ошибки", city="CityA")
    t2 = _make_topic(angle="Страх ошибки", city="CityB")
    t3 = _make_topic(angle="Результат / Показатели", city="CityA")
    t4 = _make_topic(angle="Страх ошибки", city="CityA", ad_format="carousel")

    assert _make_brief_signature(t1) != _make_brief_signature(t2)
    assert _make_brief_signature(t1) != _make_brief_signature(t3)
    assert _make_brief_signature(t1) != _make_brief_signature(t4)
    assert _make_brief_signature(t1) == "Страх ошибки::CityA::video_speaker"


# ---------------------------------------------------------------------------
# Тесты _generate_scenario_for_brief — прямой вызов LLM
# ---------------------------------------------------------------------------

def test_generate_scenario_no_api_key_returns_empty(monkeypatch):
    """Нет ANTHROPIC_API_KEY — функция возвращает пустую строку, не бросает исключение."""
    import config
    from services.brief_generator import _generate_scenario_for_brief

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)

    result = _generate_scenario_for_brief(_make_brief())

    assert result == ""


def test_generate_scenario_success(monkeypatch):
    """LLM вернул текст — функция возвращает его как есть (без обрезки/парсинга списка)."""
    import config
    from services.brief_generator import _generate_scenario_for_brief

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    fake_response = MagicMock()
    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = "Сценарий: живой текст ТЗ про бесплатную консультацию.  "
    fake_response.content = [fake_text_block]

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_response
        mock_anthropic_cls.return_value = mock_client

        result = _generate_scenario_for_brief(_make_brief())

    assert result == "Сценарий: живой текст ТЗ про бесплатную консультацию."
    assert mock_client.messages.create.called
    # thinking должен быть явно отключён — иначе первым блоком идёт ThinkingBlock
    _, call_kwargs = mock_client.messages.create.call_args
    assert call_kwargs.get("thinking") == {"type": "disabled"}


def test_generate_scenario_thinking_block_first(monkeypatch):
    """Первый блок — ThinkingBlock (без .text), сценарий должен взяться из TextBlock дальше."""
    import config
    from services.brief_generator import _generate_scenario_for_brief

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    fake_response = MagicMock()

    fake_thinking_block = MagicMock(spec=["type", "thinking"])
    fake_thinking_block.type = "thinking"
    fake_thinking_block.thinking = "рассуждения модели, не для показа"

    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = "Финальный сценарий после thinking.  "

    fake_response.content = [fake_thinking_block, fake_text_block]

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_response
        mock_anthropic_cls.return_value = mock_client

        result = _generate_scenario_for_brief(_make_brief())

    assert result == "Финальный сценарий после thinking."


def test_generate_scenario_only_thinking_block_returns_empty(monkeypatch):
    """Есть только ThinkingBlock, текстового блока нет — возвращаем '' без исключения."""
    import config
    from services.brief_generator import _generate_scenario_for_brief

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    fake_response = MagicMock()
    fake_thinking_block = MagicMock(spec=["type", "thinking"])
    fake_thinking_block.type = "thinking"
    fake_thinking_block.thinking = "только рассуждения"
    fake_response.content = [fake_thinking_block]

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_response
        mock_anthropic_cls.return_value = mock_client

        result = _generate_scenario_for_brief(_make_brief())

    assert result == ""


def test_generate_scenario_empty_content_returns_empty(monkeypatch):
    """content=[] — возвращаем '' без исключения."""
    import config
    from services.brief_generator import _generate_scenario_for_brief

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    fake_response = MagicMock()
    fake_response.content = []

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_response
        mock_anthropic_cls.return_value = mock_client

        result = _generate_scenario_for_brief(_make_brief())

    assert result == ""


def test_generate_scenario_llm_error_returns_empty(monkeypatch):
    """Ошибка Claude API — функция ловит исключение и возвращает пустую строку."""
    import config
    from services.brief_generator import _generate_scenario_for_brief

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    with patch("anthropic.Anthropic") as mock_anthropic_cls:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("Claude API таймаут")
        mock_anthropic_cls.return_value = mock_client

        result = _generate_scenario_for_brief(_make_brief())

    assert result == ""


# ---------------------------------------------------------------------------
# Тесты generate_and_push_briefs — основная функция (Сценарист v2, ARCH-phase3-
# scenarist.md §T7 + ARCH-brief-approval-flow.md): select_topics ->
# _generate_scenario_v2 -> validate_scenario -> self_critique -> очередь
# pending_briefs (status=pending) + send_with_buttons (превью с кнопками
# approve_brief:/reject_brief:). Карточка Trello создаётся ТОЛЬКО по клику
# "Одобрить" (см. tests/test_telegram_brief_approval.py) — здесь НЕ проверяется.
# ---------------------------------------------------------------------------

from services.scenario_validator import CritiqueResult, ValidationResult


def _fake_fact_sheet() -> dict:
    """Минимальный валидный Fact Sheet для тестов generate_and_push_briefs."""
    return {
        "products": {"proda": {}, "prodb": {}},
        "offer": ["Бесплатная консультация"],
        "promo_claims": ["Рассрочка банка-партнёра на 12 месяцев"],
        "social_proof_claims": [],
        "testimonials": [],
        "testimonial_mode_enabled": False,
        "incompatibility_rules": [],
        "deadlines": [],
        "allowed_numbers": [],
        "cities": ["CityA", "CityB", "CityC"],
    }


def _patch_v2_chain(mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
                     topics=None, scenario="Сценарий: живой текст про консультацию.",
                     passed=True, verdict="PASS"):
    """Настраивает мок-цепочку generate_and_push_briefs v2 на happy path."""
    mock_fact_sheet.return_value = _fake_fact_sheet()
    mock_topics.return_value = topics if topics is not None else [_make_topic()]
    mock_scenario.return_value = scenario
    mock_validate.return_value = ValidationResult(passed=passed, violations=[] if passed else ["нарушение"])
    mock_critique.return_value = CritiqueResult(verdict=verdict, reason="дуга есть, хук цепляет")


def _isolate_queue(monkeypatch, tmp_path):
    """Изолирует state-файл генератора и очередь pending_briefs на tmp_path."""
    import services.brief_generator as bg_module
    import services.pending_briefs as pb_module
    monkeypatch.setattr(bg_module, "STATE_FILE", tmp_path / "brief_gen_state.json")
    monkeypatch.setattr(pb_module, "PENDING_FILE", tmp_path / "pending_briefs.json")


# ---------------------------------------------------------------------------
# Тесты _format_brief_preview — HTML-превью ТЗ для Telegram (parse_mode=HTML,
# см. services.telegram_bot.send_with_buttons). Спецсимволы в теме/сценарии
# не должны ломать отправку (Telegram вернёт 400 Bad Request на невалидный HTML).
# ---------------------------------------------------------------------------

def test_format_brief_preview_escapes_html_and_keeps_emoji():
    """Кавычки/<>/& в названии и сценарии экранируются под parse_mode=HTML,
    эмодзи (не-ASCII, вне зоны действия html.escape) остаются как есть."""
    from services.brief_generator import _format_brief_preview

    name = 'CityA / "Топ-5" <script>alert(1)</script> 🔥'
    desc = 'Сценарий с <b>тегами</b> & "кавычками" 😀\n\n---\nслужебный блок карточки'

    preview = _format_brief_preview(name, "PRODA", desc)

    assert isinstance(preview, str)
    # "Сырые" опасные теги не должны попасть в текст — иначе Telegram (HTML) вернёт 400
    assert "<script>" not in preview
    assert "<b>тегами</b>" not in preview
    assert "&lt;script&gt;" in preview
    assert "&lt;b&gt;тегами&lt;/b&gt;" in preview
    assert "&amp;" in preview
    assert "&quot;" in preview
    # Эмодзи сохраняются как есть
    assert "🔥" in preview
    assert "😀" in preview
    # Служебный блок карточки (после "\n---\n") в превью не попадает
    assert "служебный блок карточки" not in preview


def test_format_brief_preview_handles_none_desc_and_none_product():
    """desc=None и product=None — превью не падает, продукт по умолчанию ОБЩАЯ."""
    from services.brief_generator import _format_brief_preview

    preview = _format_brief_preview("Название без сценария", None, None)

    assert isinstance(preview, str)
    assert "[ОБЩАЯ]" in preview
    assert "Название без сценария" in preview


@patch("services.product_tags._llm_classify_product", return_value="ОБЩАЯ")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_queues_pending(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_llm_classify, tmp_path, monkeypatch,
):
    """Основной сценарий v2: тема -> сценарий -> оба гейта PASS -> запись в
    очереди pending_briefs + превью с кнопками approve_brief:/reject_brief:.

    _llm_classify_product мокаем — граница сети (LLM-добор продукта, вызывается
    когда keyword-эвристика не находит совпадение, см. _classify_topic_product);
    без мока pytest-socket ловит реальный сетевой вызов (гасится try/except,
    но шумит в выводе тестов)."""
    import services.brief_generator as bg_module
    import services.pending_briefs as pb_module
    _isolate_queue(monkeypatch, tmp_path)

    _patch_v2_chain(mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
                     topics=[_make_topic(angle="Рассрочка банка-партнёра на 12 месяцев"), _make_topic(angle="3 услуги", city="CityB")])
    mock_buttons.return_value = True

    result = bg_module.generate_and_push_briefs(max_briefs=2)

    assert result["queued"] == 2 and result["created"] == 2
    assert result["error"] is None
    assert mock_buttons.call_count == 2
    # callback_data кнопок — approve_brief:/reject_brief:
    first_buttons = mock_buttons.call_args_list[0][0][1]
    labels_cbs = [cb for row in first_buttons for _, cb in row]
    assert any(c.startswith("approve_brief:") for c in labels_cbs)
    assert any(c.startswith("reject_brief:") for c in labels_cbs)
    assert len(pb_module._load()["briefs"]) == 2


@patch("services.product_tags._llm_classify_product", return_value="ОБЩАЯ")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_no_duplicate(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_llm_classify, tmp_path, monkeypatch,
):
    """Дубликаты не создаются: повторный вызов с той же темой пропускает её
    (дедуп через active_signatures() очереди pending_briefs). _llm_classify_product
    мокаем — граница сети LLM-добора продукта (см. test_generate_and_push_queues_pending)."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    _patch_v2_chain(mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique)
    mock_buttons.return_value = True

    # Первый вызов — кладёт в очередь единственную тему
    result1 = bg_module.generate_and_push_briefs(max_briefs=10)
    assert result1["queued"] == 1

    # Второй вызов — та же тема уже pending в очереди (та же сигнатура angle::city::ad_format)
    result2 = bg_module.generate_and_push_briefs(max_briefs=10)
    assert result2["queued"] == 0
    assert result2["skipped"] >= 1


@patch("services.brief_generator.send_telegram")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_no_topics(mock_fact_sheet, mock_topics, mock_telegram, tmp_path, monkeypatch):
    """Нет тем (покрытие в норме, победителей нет) — возвращает ошибку, ничего не создаёт."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    mock_fact_sheet.return_value = _fake_fact_sheet()
    mock_topics.return_value = []

    result = bg_module.generate_and_push_briefs()

    assert result["created"] == 0
    assert result["error"] is not None
    assert not mock_telegram.called


@patch("services.product_tags._llm_classify_product", return_value="ОБЩАЯ")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_preview_sent(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_llm_classify, tmp_path, monkeypatch,
):
    """Превью с кнопками отправляется через send_with_buttons при успешной
    постановке ТЗ в очередь (НЕ send_telegram — это блок-алертов не про этот путь).
    _llm_classify_product мокаем — граница сети LLM-добора продукта."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    _patch_v2_chain(mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique)
    mock_buttons.return_value = True

    result = bg_module.generate_and_push_briefs(max_briefs=2)

    assert result["queued"] > 0
    assert mock_buttons.called
    # Одна строка кнопок с двумя кнопками (Одобрить/Отклонить)
    buttons_arg = mock_buttons.call_args[0][1]
    assert len(buttons_arg) == 1
    assert len(buttons_arg[0]) == 2


@patch("services.product_tags._llm_classify_product", return_value="ОБЩАЯ")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_max_briefs_respected(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_llm_classify, tmp_path, monkeypatch,
):
    """Лимит max_briefs соблюдается даже когда select_topics вернул избыточный пул
    тем (фикс 2026-07-09: пул запрашивается БОЛЬШЕ max_briefs — _TOPIC_POOL_SIZE —
    чтобы было из чего добирать NEXT тему при дубле, см. test_generate_and_push_
    duplicate_falls_through_to_next_topic). Очередь всё равно ограничена max_briefs.
    _llm_classify_product мокаем — граница сети LLM-добора продукта."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    # Пул из 3 РАЗНЫХ (недублирующихся) тем, max_briefs=1 — в очередь должна
    # уйти только первая по рангу, остальные даже не трогаются (LLM не звался).
    pool = [
        _make_topic(angle="Тема 1", city="CityA"),
        _make_topic(angle="Тема 2", city="CityB"),
        _make_topic(angle="Тема 3", city="CityC"),
    ]
    _patch_v2_chain(mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
                     topics=pool)
    mock_buttons.return_value = True

    result = bg_module.generate_and_push_briefs(max_briefs=1)

    assert result["queued"] == 1
    assert result["angles"] == ["Тема 1"]
    mock_scenario.assert_called_once()  # LLM вызван РОВНО один раз — не для всех 3 тем пула
    mock_topics.assert_called_once_with(max_topics=bg_module._TOPIC_POOL_SIZE)


# ---------------------------------------------------------------------------
# Добор тем при дублях: раньше при дубле темы среди верхних по
# рангу (уже в state/активной очереди) прогон уходил с created=0, хотя
# select_topics вернул избыточный пул (_TOPIC_POOL_SIZE) — ниже по рангу были
# ещё непросмотренные (не-дублирующиеся) темы. Теперь при дубле берётся
# СЛЕДУЮЩАЯ тема по рангу, пока не наберётся max_briefs НЕ-дублей или пул не
# кончится. Телеметрия topics_seen/topics_skipped_dup — в результате.
# ---------------------------------------------------------------------------

@patch("services.product_tags._llm_classify_product", return_value="ОБЩАЯ")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_duplicate_falls_through_to_next_topic(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_llm_classify, tmp_path, monkeypatch,
):
    """Первая тема пула — дубль (уже pending в очереди), вторая — свежая: генератор
    добирает вторую тему, created=1 (регрессия до фикса: created=0, весь прогон
    уходил ни с чем на первом же дубле)."""
    import services.brief_generator as bg_module
    import services.pending_briefs as pb_module
    _isolate_queue(monkeypatch, tmp_path)

    topic_dup = _make_topic(angle="Уже в очереди", city="CityA")
    topic_fresh = _make_topic(angle="Новая тема", city="CityB")
    sig_dup = bg_module._make_brief_signature(topic_dup)
    pb_module.add_pending("CityA / Уже в очереди", "desc", None, sig_dup)

    _patch_v2_chain(mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
                     topics=[topic_dup, topic_fresh])
    mock_buttons.return_value = True

    result = bg_module.generate_and_push_briefs(max_briefs=1)

    assert result["created"] == 1
    assert result["queued"] == 1
    assert result["angles"] == ["Новая тема"]
    assert result["error"] is None
    assert result["topics_seen"] == 2
    assert result["topics_skipped_dup"] == 1
    mock_scenario.assert_called_once()  # сценарий генерировался только для свежей темы


@patch("services.brief_generator.send_telegram")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_all_pool_duplicates_no_error_raised(
    mock_fact_sheet, mock_topics, mock_telegram, tmp_path, monkeypatch,
):
    """Весь пул select_topics — дубли (уже pending в очереди): created=0, честный
    результат без исключений — не звоним LLM/self_critique впустую, не шлём
    Telegram-алерт о блоке (это не блок validate_scenario, а обычный дедуп).
    Телеметрия отражает весь просмотренный пул."""
    import services.brief_generator as bg_module
    import services.pending_briefs as pb_module
    _isolate_queue(monkeypatch, tmp_path)

    topic1 = _make_topic(angle="Дубль 1", city="CityA")
    topic2 = _make_topic(angle="Дубль 2", city="CityB")
    sig1 = bg_module._make_brief_signature(topic1)
    sig2 = bg_module._make_brief_signature(topic2)
    pb_module.add_pending("CityA / Дубль 1", "desc", None, sig1)
    pb_module.add_pending("CityB / Дубль 2", "desc", None, sig2)

    mock_fact_sheet.return_value = _fake_fact_sheet()
    mock_topics.return_value = [topic1, topic2]

    result = bg_module.generate_and_push_briefs(max_briefs=3)  # не должно бросить исключение

    assert result["created"] == 0
    assert result["queued"] == 0
    assert result["blocked"] == 0
    assert result["error"] == "Все темы уже созданы (дубли)"
    assert result["topics_seen"] == 2
    assert result["topics_skipped_dup"] == 2
    assert not mock_telegram.called


@patch("services.product_tags._llm_classify_product", return_value="ОБЩАЯ")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_telemetry_present_on_success(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_llm_classify, tmp_path, monkeypatch,
):
    """topics_seen/topics_skipped_dup присутствуют в результате даже когда дублей
    вообще не было (обычный happy path, без дублей в пуле)."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    _patch_v2_chain(mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
                     topics=[_make_topic()])
    mock_buttons.return_value = True

    result = bg_module.generate_and_push_briefs(max_briefs=1)

    assert result["topics_seen"] == 1
    assert result["topics_skipped_dup"] == 0


@patch("services.brief_generator.send_telegram")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_blocked_by_validator(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_telegram,
    tmp_path, monkeypatch,
):
    """validate_scenario не прошёл — в очередь НЕ кладём, blocked++, Telegram-алерт (AC6, §8)."""
    import services.brief_generator as bg_module
    import services.pending_briefs as pb_module
    _isolate_queue(monkeypatch, tmp_path)

    _patch_v2_chain(mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
                     passed=False)

    result = bg_module.generate_and_push_briefs(max_briefs=1)

    assert result["queued"] == 0
    assert result["blocked"] == 1
    assert len(result["blocks"]) == 1
    assert not mock_buttons.called
    assert mock_telegram.called  # алерт "ТЗ заблокировано: ..."
    assert len(pb_module._load()["briefs"]) == 0


@patch("services.brief_generator.send_telegram")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_blocked_by_self_critique(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_telegram,
    tmp_path, monkeypatch,
):
    """self_critique вернул BLOCK — в очередь НЕ кладём, blocked++, Telegram-алерт."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    _patch_v2_chain(mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
                     verdict="BLOCK")

    result = bg_module.generate_and_push_briefs(max_briefs=1)

    assert result["queued"] == 0
    assert result["blocked"] == 1
    assert not mock_buttons.called
    assert mock_telegram.called


# ---------------------------------------------------------------------------
# Фикс 2026-07-09: параметр trigger="scheduled"|"manual" у generate_and_push_briefs
# управляет тем, в какой ключ state пишется метка последнего запуска. Ручной
# триггер (дефолт, реальные вызывающие места — /brief в telegram_console и HTTP
# /api/autopilot/generate-briefs-now) НЕ должен писать last_scheduled_run_date
# — иначе should_run_generator ошибочно решит, что крон уже отработал.
# ---------------------------------------------------------------------------

@patch("services.brief_generator.send_telegram")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_manual_trigger_writes_last_manual_run_date_not_scheduled(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_telegram,
    tmp_path, monkeypatch,
):
    """trigger по умолчанию "manual" (как у /brief и HTTP-эндпоинта) пишет
    ТОЛЬКО last_manual_run_date — last_scheduled_run_date (то, что читает
    интервальный гейт should_run_generator) не трогает."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    _patch_v2_chain(mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
                     passed=False)

    result = bg_module.generate_and_push_briefs(max_briefs=1)  # trigger не передан -> "manual"

    assert result["blocked"] == 1
    state = bg_module._load_state()
    assert state["last_manual_run_date"] is not None
    assert state["last_scheduled_run_date"] is None


@patch("services.brief_generator.send_telegram")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_scheduled_trigger_writes_last_scheduled_run_date_not_manual(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_telegram,
    tmp_path, monkeypatch,
):
    """trigger="scheduled" (как у крона _cron_brief_generator) пишет ТОЛЬКО
    last_scheduled_run_date — last_manual_run_date не трогает."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    _patch_v2_chain(mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
                     passed=False)

    result = bg_module.generate_and_push_briefs(max_briefs=1, trigger="scheduled")

    assert result["blocked"] == 1
    state = bg_module._load_state()
    assert state["last_scheduled_run_date"] is not None
    assert state["last_manual_run_date"] is None


def test_manual_generate_yesterday_then_scheduled_gate_passes_today(tmp_path, monkeypatch):
    """Регрессия бага 2026-07-09 от начала до конца: ручной generate_and_push_briefs
    "вчера" (симулируем через прямую запись state) не блокирует плановый
    прогон сегодня — should_run_generator видит last_manual_run_date, но не
    last_scheduled_run_date, и пропускает крон."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    now = datetime.now(_TZ_LOCAL)
    yesterday = now - timedelta(days=1)

    # Симулируем вчерашний ручной запуск (тот путь, что писал бы
    # generate_and_push_briefs(trigger="manual") вчера)
    bg_module._save_state({
        "last_scheduled_run_date": None,
        "last_manual_run_date": yesterday.isoformat(),
        "generated_signatures": [],
    })

    state_today = bg_module._load_state()
    assert bg_module.should_run_generator(now, state_today) is True


# ---------------------------------------------------------------------------
# Тест state-файла
# ---------------------------------------------------------------------------

def test_load_save_state(tmp_path, monkeypatch):
    """_load_state и _save_state корректно читают и пишут файл.

    Фикс 2026-07-09: последний запуск теперь хранится в двух отдельных ключах
    (last_scheduled_run_date/last_manual_run_date) вместо единого last_run_date
    — ручной прогон не должен красть слот у планового (should_run_generator)."""
    import services.brief_generator as bg_module
    state_path = tmp_path / "brief_gen_state.json"
    monkeypatch.setattr(bg_module, "STATE_FILE", state_path)

    # Пустой файл → дефолт
    state = bg_module._load_state()
    assert state["last_scheduled_run_date"] is None
    assert state["last_manual_run_date"] is None
    assert state["generated_signatures"] == []

    # Сохраняем
    state["last_scheduled_run_date"] = "2026-06-25T08:00:00"
    state["last_manual_run_date"] = "2026-06-24T09:00:00"
    state["generated_signatures"] = ["Страх ошибки::CityA"]
    bg_module._save_state(state)

    # Перечитываем
    loaded = bg_module._load_state()
    assert loaded["last_scheduled_run_date"] == "2026-06-25T08:00:00"
    assert loaded["last_manual_run_date"] == "2026-06-24T09:00:00"
    assert "Страх ошибки::CityA" in loaded["generated_signatures"]


def test_load_state_migrates_legacy_last_run_date(tmp_path, monkeypatch):
    """Старый прод-state с единым last_run_date (до фикса 2026-07-09) мигрирует
    без падения: значение трактуется как last_scheduled_run_date."""
    import services.brief_generator as bg_module
    state_path = tmp_path / "brief_gen_state.json"
    monkeypatch.setattr(bg_module, "STATE_FILE", state_path)

    legacy_state = {"last_run_date": "2026-07-06T08:15:00", "generated_signatures": ["sig1"]}
    state_path.write_text(json.dumps(legacy_state), encoding="utf-8")

    loaded = bg_module._load_state()

    assert loaded["last_scheduled_run_date"] == "2026-07-06T08:15:00"
    assert loaded["last_manual_run_date"] is None
    assert loaded["generated_signatures"] == ["sig1"]


# ---------------------------------------------------------------------------
# Тесты _latest_brief_run_date — регрессия 2026-07-09: evening_report.py и
# morning_digest.py читают state напрямую (без should_run_generator) для
# секции «Карточки ТЗ» — раньше смотрели на единый last_run_date, после
# разделения на last_scheduled_run_date/last_manual_run_date секции молча
# переставали видеть прогоны. _latest_brief_run_date — общая функция для
# обоих отчётов: свежайшая из двух новых меток, fallback на legacy-ключ.
# ---------------------------------------------------------------------------

def test_latest_brief_run_date_only_new_keys():
    """State только с новыми ключами (одна метка задана) — возвращает её."""
    from services.brief_generator import _latest_brief_run_date

    state = {"last_scheduled_run_date": "2026-07-06T08:00:00", "last_manual_run_date": None}
    assert _latest_brief_run_date(state) == "2026-07-06T08:00:00"

    state2 = {"last_scheduled_run_date": None, "last_manual_run_date": "2026-07-06T14:00:00"}
    assert _latest_brief_run_date(state2) == "2026-07-06T14:00:00"


def test_latest_brief_run_date_legacy_key_fallback():
    """State со старым ключом last_run_date (без новых ключей вообще) — обратная
    совместимость: значение возвращается как есть."""
    from services.brief_generator import _latest_brief_run_date

    state = {"last_run_date": "2026-07-01T09:00:00", "generated_signatures": []}
    assert _latest_brief_run_date(state) == "2026-07-01T09:00:00"


def test_latest_brief_run_date_both_keys_picks_later():
    """Оба ключа заданы — берётся более поздний по времени (не порядок в словаре)."""
    from services.brief_generator import _latest_brief_run_date

    state = {"last_scheduled_run_date": "2026-07-01T08:00:00", "last_manual_run_date": "2026-07-06T14:00:00"}
    assert _latest_brief_run_date(state) == "2026-07-06T14:00:00"

    # Порядок инвертирован — scheduled свежее manual, всё равно берём scheduled
    state2 = {"last_scheduled_run_date": "2026-07-06T14:00:00", "last_manual_run_date": "2026-07-01T08:00:00"}
    assert _latest_brief_run_date(state2) == "2026-07-06T14:00:00"


def test_latest_brief_run_date_mixed_naive_and_aware_does_not_raise():
    """Регрессия: крон в web/app.py пишет last_scheduled_run_date aware-датой
    (now.isoformat() с tz), а generate_and_push_briefs — naive
    datetime.now().isoformat(). Прямое сравнение падало TypeError «can't
    compare offset-naive and offset-aware datetimes» — отчёты ловили его и
    молча показывали 0 карточек ТЗ. Обе стороны приводятся к aware UTC;
    побеждает реально более свежая метка, а не та, у которой есть tz."""
    from services.brief_generator import _latest_brief_run_date

    # aware свежее naive
    state = {
        "last_scheduled_run_date": "2026-07-06T14:00:00+05:00",
        "last_manual_run_date": "2026-07-01T08:00:00",
    }
    assert _latest_brief_run_date(state) == "2026-07-06T14:00:00+05:00"

    # naive свежее aware (aware недельной давности не должна выигрывать)
    state2 = {
        "last_scheduled_run_date": "2026-06-20T08:00:05+05:00",
        "last_manual_run_date": "2026-07-01T20:00:00",
    }
    assert _latest_brief_run_date(state2) == "2026-07-01T20:00:00"

    # naive трактуется как локальное время (UTC+5): 09:00 naive == 04:00Z, и aware
    # 04:30Z того же дня — свежее на полчаса.
    state3 = {
        "last_scheduled_run_date": "2026-07-01T04:30:00+00:00",
        "last_manual_run_date": "2026-07-01T09:00:00",
    }
    assert _latest_brief_run_date(state3) == "2026-07-01T04:30:00+00:00"

    # Битая метка рядом с aware-валидной — не падает, побеждает валидная
    state4 = {
        "last_scheduled_run_date": "не дата",
        "last_manual_run_date": "2026-07-01T09:00:00+05:00",
    }
    assert _latest_brief_run_date(state4) == "2026-07-01T09:00:00+05:00"


def test_latest_brief_run_date_no_runs_returns_none():
    """Ни одного ключа не задано (свежий state без прогонов) — None, не падает."""
    from services.brief_generator import _latest_brief_run_date

    assert _latest_brief_run_date({}) is None
    assert _latest_brief_run_date({"generated_signatures": []}) is None


# ---------------------------------------------------------------------------
# Тест create_card в integrations/trello.py
# ---------------------------------------------------------------------------

@patch("integrations.trello.session.post")
def test_create_card_calls_trello_api(mock_post):
    """create_card делает POST запрос в Trello."""
    mock_post.return_value = MagicMock(
        status_code=200,
        json=lambda: {"id": "card123", "url": "https://trello.com/c/card123"},
    )
    mock_post.return_value.raise_for_status = lambda: None

    from integrations.trello import create_card

    result = create_card("list_id_test", "Тест карточка", "Описание теста")

    assert result["id"] == "card123"
    assert mock_post.called
    # payload уходит в тело запроса (data=), а не в query string (params=) —
    # иначе длинный desc реального сценария превышает лимит URL и Trello
    # отвечает 414 Request-URI Too Large
    call_data = mock_post.call_args[1]["data"]
    assert call_data["idList"] == "list_id_test"
    assert call_data["name"] == "Тест карточка"


# ---------------------------------------------------------------------------
# Тест: пустой сценарий от LLM → в очередь НЕ кладём (лучше 0, чем мусор)
# ---------------------------------------------------------------------------

@patch("services.brief_generator.send_telegram")
@patch("services.brief_generator.send_with_buttons")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_skips_when_llm_returns_empty(
    mock_fact_sheet, mock_topics, mock_scenario, mock_buttons, mock_telegram,
    tmp_path, monkeypatch,
):
    """LLM недоступен / вернул пустой сценарий — тема пропускается,
    send_with_buttons НЕ вызывается, result["skipped"] растёт."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    mock_fact_sheet.return_value = _fake_fact_sheet()
    mock_topics.return_value = [_make_topic()]
    mock_scenario.return_value = ""  # LLM не вернул сценарий (нет ключа/ошибка)

    result = bg_module.generate_and_push_briefs(max_briefs=10)

    assert result["queued"] == 0
    assert result["skipped"] >= 1
    assert not mock_buttons.called
    assert not mock_telegram.called


@patch("services.brief_generator.send_telegram")
@patch("services.brief_generator.send_with_buttons")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_skips_when_llm_returns_none_like(
    mock_fact_sheet, mock_topics, mock_scenario, mock_buttons, mock_telegram,
    tmp_path, monkeypatch,
):
    """Сценарий из пробелов (whitespace-only) — тоже считается пустым и пропускается."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    mock_fact_sheet.return_value = _fake_fact_sheet()
    mock_topics.return_value = [_make_topic()]
    mock_scenario.return_value = "   \n  "

    result = bg_module.generate_and_push_briefs(max_briefs=10)

    assert result["queued"] == 0
    assert result["skipped"] >= 1
    assert not mock_buttons.called


# ---------------------------------------------------------------------------
# Тест: Fact Sheet недоступен → fail-closed, карточек не создаём (§8 спеки)
# ---------------------------------------------------------------------------

@patch("services.brief_generator.send_telegram")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_fact_sheet_unavailable_fail_closed(mock_fact_sheet, mock_telegram, tmp_path, monkeypatch):
    """FactSheetError при загрузке Fact Sheet — карточек не создаём, error заполнен."""
    from services.fact_sheet import FactSheetError
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    mock_fact_sheet.side_effect = FactSheetError("источник .md не найден")

    result = bg_module.generate_and_push_briefs()

    assert result["created"] == 0
    assert result["error"] is not None
    assert not mock_telegram.called


# ---------------------------------------------------------------------------
# T10 (доп.): смешанный прогон — 1 успех + 1 блок + 1 пустой LLM за один вызов.
# Счётчики created/skipped/blocked должны отражать все три исхода одновременно
# (§9 спеки: "счётчики created/skipped/blocked в результате корректны при
# смешанном прогоне").
# ---------------------------------------------------------------------------

@patch("services.product_tags._llm_classify_product", return_value="ОБЩАЯ")
@patch("services.brief_generator.send_telegram")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_mixed_run_counts_all_outcomes(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_telegram, mock_llm_classify,
    tmp_path, monkeypatch,
):
    """3 темы за один прогон: 1-я — LLM вернул пустоту (skip), 2-я — validate_scenario
    блокирует (blocked), 3-я — проходит оба гейта (queued). Итог: queued=1,
    skipped=1, blocked=1 — ни один исход не "съедает" другой.
    _llm_classify_product мокаем — граница сети LLM-добора продукта."""
    import services.brief_generator as bg_module
    import services.pending_briefs as pb_module
    _isolate_queue(monkeypatch, tmp_path)

    topic_empty = _make_topic(angle="Пустой LLM", city="CityA")
    topic_blocked = _make_topic(angle="Заблокированная тема", city="CityB")
    topic_ok = _make_topic(angle="Успешная тема", city="CityC")

    mock_fact_sheet.return_value = _fake_fact_sheet()
    mock_topics.return_value = [topic_empty, topic_blocked, topic_ok]

    # LLM: пусто для 1-й темы, валидный текст для 2-й и 3-й
    mock_scenario.side_effect = [
        "",
        "Сценарий: заблокированный текст.",
        "Сценарий: успешный текст.",
    ]
    # validate_scenario: блок для 2-й темы (для 3-й — не вызывается для 1-й,
    # т.к. пустой сценарий пропускается раньше валидатора)
    mock_validate.side_effect = [
        ValidationResult(passed=False, violations=["выдуманное число"]),
        ValidationResult(passed=True, violations=[]),
    ]
    mock_critique.return_value = CritiqueResult(verdict="PASS", reason="дуга есть")
    mock_buttons.return_value = True

    result = bg_module.generate_and_push_briefs(max_briefs=3)

    assert result["queued"] == 1
    assert result["skipped"] == 1
    assert result["blocked"] == 1
    assert result["angles"] == ["Успешная тема"]
    assert len(result["blocks"]) == 1
    assert result["blocks"][0]["topic"] == "Заблокированная тема"
    # В очереди — только успешная тема
    assert len(pb_module._load()["briefs"]) == 1
    mock_buttons.assert_called_once()


# ---------------------------------------------------------------------------
# T10 (доп.): _generate_scenario_v2 логирует LLM-вызов через llm_logger с
# purpose="scenario_generate" (AC9 спеки) — проверяем саму функцию напрямую,
# не через мок в generate_and_push_briefs (там _generate_scenario_v2 замокан
# целиком и не проверяет своё внутреннее логирование).
# ---------------------------------------------------------------------------

def test_generate_scenario_v2_logs_llm_call_with_scenario_generate_purpose(monkeypatch):
    """Успешная генерация сценария v2 логирует вызов с purpose=scenario_generate."""
    import config
    from services.brief_generator import _generate_scenario_v2

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-key")

    fake_response = MagicMock()
    fake_text_block = MagicMock()
    fake_text_block.type = "text"
    fake_text_block.text = "Сценарий: живой текст про консультацию."
    fake_response.content = [fake_text_block]
    fake_response.usage = MagicMock(input_tokens=100, output_tokens=50)

    with patch("anthropic.Anthropic") as mock_anthropic_cls, \
         patch("services.llm_logger.log_llm_call") as mock_log_call:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_response
        mock_anthropic_cls.return_value = mock_client

        result = _generate_scenario_v2(_make_topic(), _fake_fact_sheet())

    assert result == "Сценарий: живой текст про консультацию."
    mock_log_call.assert_called_once()
    _, call_kwargs = mock_log_call.call_args
    assert call_kwargs.get("purpose") == "scenario_generate"


# ---------------------------------------------------------------------------
# T10 (доп.): дубликат темы блокирует ЭТУ ЖЕ тему на следующем прогоне даже
# если тема была заблокирована валидатором/самокритикой (не только успешно
# созданная) — сигнатура пишется в state в обоих случаях (см. §T7 код:
# new_sigs.append(sig) в ветках blocked). Проверяем именно "заблокированная
# тема запоминается в state (не пересобирается на следующем прогоне)".
#
# ФИКС (ARCH-brief-approval-flow.md T7): декораторы @patch create_card/
# get_or_create_list убраны — этих атрибутов больше нет в brief_generator
# (тема блокируется валидатором, Trello-моки ей не нужны). Добавлена изоляция
# PENDING_FILE. Логика проверок (blocked==1, 2-й прогон skipped, mock_scenario
# не вызван) НЕ изменена.
# ---------------------------------------------------------------------------

@patch("services.brief_generator.send_telegram")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_blocked_topic_remembered_not_regenerated(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_telegram,
    tmp_path, monkeypatch,
):
    """Тема заблокирована в 1-м прогоне -> во 2-м прогоне с той же темой она
    пропускается как дубль (skipped), LLM/валидатор повторно НЕ вызываются."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)

    topic = _make_topic(angle="Опасная тема", city="CityA")
    mock_fact_sheet.return_value = _fake_fact_sheet()
    mock_topics.return_value = [topic]
    mock_scenario.return_value = "Сценарий: текст с нарушением."
    mock_validate.return_value = ValidationResult(passed=False, violations=["смешение голосов"])

    # 1-й прогон — тема заблокирована, её сигнатура попадает в state
    result1 = bg_module.generate_and_push_briefs(max_briefs=1)
    assert result1["blocked"] == 1
    assert result1["created"] == 0

    # 2-й прогон — та же тема уже в state, повторно сценарий не генерируется
    mock_scenario.reset_mock()
    result2 = bg_module.generate_and_push_briefs(max_briefs=1)

    assert result2["skipped"] >= 1
    assert result2["blocked"] == 0
    assert result2["created"] == 0
    assert not mock_scenario.called


# ---------------------------------------------------------------------------
# Новые тесты ARCH-brief-approval-flow.md: ретеншн очереди + дедуп по pending
# ---------------------------------------------------------------------------

def test_generate_and_push_expires_old_pending(tmp_path, monkeypatch):
    """pending-запись 15-дневной давности в очереди -> после прогона генератора
    (даже если сама генерация ничего не создаёт) она помечена expired."""
    import services.brief_generator as bg_module
    import services.pending_briefs as pb_module
    _isolate_queue(monkeypatch, tmp_path)

    old_time = datetime.now(_TZ_LOCAL) - timedelta(days=pb_module.RETENTION_DAYS + 1)
    stale = pb_module.add_pending("Старая тема", "desc", None, "sig-old", now=old_time)

    with patch("services.fact_sheet.load_fact_sheet", return_value=_fake_fact_sheet()), \
         patch("services.topic_selector.select_topics", return_value=[_make_topic(angle="Новая тема")]), \
         patch("services.brief_generator._generate_scenario_v2", return_value=""):
        bg_module.generate_and_push_briefs(max_briefs=1)

    assert pb_module.get_brief(stale["id"])["status"] == pb_module.STATUS_EXPIRED


def test_generate_and_push_dedup_sees_pending_queue(tmp_path, monkeypatch):
    """Тема с сигнатурой уже лежит pending в очереди (без записи в state) —
    генератор её пропускает (skipped), send_with_buttons не вызывается."""
    import services.brief_generator as bg_module
    _isolate_queue(monkeypatch, tmp_path)
    import services.pending_briefs as pb_module

    topic = _make_topic(angle="Уже в очереди", city="CityA")
    sig = bg_module._make_brief_signature(topic)
    pb_module.add_pending("CityA / Уже в очереди", "desc", None, sig)

    with patch("services.fact_sheet.load_fact_sheet", return_value=_fake_fact_sheet()), \
         patch("services.topic_selector.select_topics", return_value=[topic]), \
         patch("services.brief_generator.send_with_buttons") as mock_buttons:
        result = bg_module.generate_and_push_briefs(max_briefs=1)

    assert result["queued"] == 0
    assert result["skipped"] >= 1
    assert not mock_buttons.called


@patch("services.product_tags._llm_classify_product", return_value="ОБЩАЯ")
@patch("services.brief_generator.send_with_buttons")
@patch("services.scenario_validator.self_critique")
@patch("services.scenario_validator.validate_scenario")
@patch("services.brief_generator._generate_scenario_v2")
@patch("services.topic_selector.select_topics")
@patch("services.fact_sheet.load_fact_sheet")
def test_generate_and_push_survives_corrupt_pending_file(
    mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
    mock_buttons, mock_llm_classify, tmp_path, monkeypatch,
):
    """Битый JSON в pending_briefs.json ДО прогона не роняет генератор:
    expire_old()/active_signatures() деградируют на пустую очередь (через
    pending_briefs._load()), add_pending перезаписывает файл валидным
    содержимым. Итог: тема всё равно уходит в очередь на одобрение.
    _llm_classify_product мокаем — граница сети LLM-добора продукта."""
    import services.brief_generator as bg_module
    import services.pending_briefs as pb_module
    _isolate_queue(monkeypatch, tmp_path)
    pb_module.PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    pb_module.PENDING_FILE.write_text("{битый джейсон, не парсится", encoding="utf-8")

    _patch_v2_chain(mock_fact_sheet, mock_topics, mock_scenario, mock_validate, mock_critique,
                     topics=[_make_topic(angle="Тема после битого файла")])
    mock_buttons.return_value = True

    result = bg_module.generate_and_push_briefs(max_briefs=1)  # не должно бросить исключение

    assert result["queued"] == 1
    assert result["created"] == 1
    assert result["error"] is None
    briefs = pb_module._load()["briefs"]
    assert len(briefs) == 1
    assert briefs[0]["status"] == pb_module.STATUS_PENDING
    assert briefs[0]["name"]
