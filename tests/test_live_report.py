"""
Тесты Live-отчёта автопилота (services/autopilot.py) — редизайн
(ARCH-live-report-redesign): бизнес-причина впереди, квал/оплаты видны,
score и творческий жаргон (hook/CTR) в Telegram больше не выводятся.

Проверяем _format_live_pause_block (один блок) и _send_live_telegram_report
(что уходит владельцу из слота, а что молчит).

Контракт слот-отчёта сменился (решение владельца
«опять пришли предложения»): днём — тишина, предложения копятся в дайджест
9:00, наружу из слота прорываются только ошибки. Поэтому тесты заголовка
«предлагаю паузу N» и «аутсайдеров нет» здесь больше не живут — сообщения,
которое они проверяли, нет. Инварианты не потеряны, а переехали туда, где
теперь строится текст для владельца:
  * лимит 4096 и отсутствие жаргона в списке пауз — tests/test_autonomous_pause.py
    (test_daily_summary_fits_telegram_limit, ...has_no_score_or_creative_jargon);
  * формат самого блока — тесты _format_live_pause_block ниже, они не менялись.
Здесь остаётся контракт отправки: когда слот молчит и что именно уходит в
единственном говорящем сообщении — ошибки вместе с секцией «Держу».
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта (чужой незакоммиченный код,
# как в tests/test_pause_report.py и tests/test_autopilot.py)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from services.autopilot import (
    _format_live_pause_block,
    _send_live_telegram_report,
    _TELEGRAM_MAX_LEN,
)


def _held(**overrides) -> dict:
    """Тестовая позиция held — реклама под удержанием."""
    base = {
        "ad_id": "ad_h",
        "ad_name": "Удержанная реклама",
        "hold_until": "2026-08-28T09:00:00+05:00",
        "romi": 184.0,
        "spend": 512.0,
        "qual_pct": None,
    }
    base.update(overrides)
    return base


def _detail(**overrides) -> dict:
    """Тестовая позиция paused_details (контракт §6.2 ARCH-live-report-redesign)."""
    base = {
        "id": "ad1",
        "name": "CityA / Петров / Тема А",
        "reason": "PAUSE: есть лиды, но qual_pct=0 (нет квалов)",
        "business_reason": "8 лидов, ни одного квала",
        "score": 3,
        "spend": 72.03,
        "leads": 8,
        "cpl": 8.25,
        "qual_pct": 0.0,
        "payments": 0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _format_live_pause_block — один блок
# ---------------------------------------------------------------------------

def test_live_block_full_data():
    """Полные данные — ровно 4 строки, деньги через fmt_money, квал/оплаты видны, НЕТ score."""
    d = _detail(spend=171, leads=30, cpl=5.7, qual_pct=20.0, payments=0,
                business_reason="слабее похожих реклам в своей группе (город/тип)")
    block = _format_live_pause_block(1, d)
    lines = block.split("\n")

    assert len(lines) == 4, f"Ожидали 4 строки, получили {len(lines)}: {block}"
    assert "$171" in block
    assert "30 лидов" in block
    assert "CPL $5.7" in block
    assert "квал 6 (20%)" in block  # round(20/100*30) = 6
    assert "оплат 0" in block
    assert "слабее похожих реклам" in block
    assert "score" not in block.lower()


def test_live_block_qual_none_says_no_data():
    """qual_pct=None и payments=None → «квал нет данных» / «оплат нет данных» (не 0, не —)."""
    d = _detail(qual_pct=None, payments=None)
    block = _format_live_pause_block(2, d)

    assert "квал нет данных" in block
    assert "оплат нет данных" in block
    assert "квал 0" not in block
    assert "квал —" not in block
    assert "оплат —" not in block


def test_live_block_qual_zero_shows_zero():
    """qual_pct=0.0 (реальный ноль) → «квал 0 (0%)», не «нет данных»."""
    d = _detail(qual_pct=0.0, leads=8)
    block = _format_live_pause_block(1, d)

    assert "квал 0 (0%)" in block


def test_live_block_business_reason_first():
    """Строка 📉 — это business_reason (не сырой reason с творческими сигналами)."""
    d = _detail(
        business_reason="8 лидов, ни одного квала",
        reason="hook/ctr не выше медианы группы; +2: видео широкий вход; PAUSE: есть лиды, но qual_pct=0",
    )
    block = _format_live_pause_block(1, d)
    reason_line = [line for line in block.split("\n") if "📉" in line][0]

    assert "8 лидов, ни одного квала" in reason_line
    assert "hook" not in block.lower()
    assert "ctr" not in block.lower()


def test_live_block_long_reason_word_truncated():
    """business_reason длиннее 200 символов — обрезан по границе слова, не посреди слова."""
    long_reason = " ".join(["слово"] * 60)  # 60*6 = 360 символов
    d = _detail(business_reason=long_reason)
    block = _format_live_pause_block(1, d)

    assert block.endswith("…")
    # Обрезка не должна рвать слово "слово" пополам
    reason_line = [line for line in block.split("\n") if "📉" in line][0]
    assert "слов…" not in reason_line and "сло…" not in reason_line


def test_live_block_name_word_truncated():
    """Имя длиннее 60 символов режется по границе слова через truncate_at_word_boundary."""
    long_name = "CityA / Петров / " + " ".join(["Подтема"] * 10)
    d = _detail(name=long_name)
    block = _format_live_pause_block(1, d)
    name_line = block.split("\n")[0]

    assert name_line.endswith("…")
    assert "Подтем…" not in name_line  # не обрубок посреди слова


def test_live_block_money_via_fmt_money():
    """Крупная сумма форматируется через fmt_money (точка-разделитель тысяч)."""
    d = _detail(spend=1500)
    block = _format_live_pause_block(1, d)

    assert "$1.500" in block


# ---------------------------------------------------------------------------
# _send_live_telegram_report — что уходит из слота, а что молчит
# ---------------------------------------------------------------------------

@patch("services.telegram_bot.send_with_buttons", return_value=True)
@patch("services.notifications.send_critical_alert")
@patch("services.notifications.send_telegram")
def test_live_report_silent_when_proposing_pauses(_send_telegram, _send_alert, mock_buttons):
    """Предложения пауз из слота не уходят вовсе — ни текстом, ни кнопками.

    Решение владельца: список «предлагаю паузу — жду решения» каждые
    2 часа он назвал спамом. Предложения ждут дайджеста 9:00 (owner_delivery),
    и слот обязан молчать, даже когда кандидаты есть.
    """
    details = [_detail(id="ad1"), _detail(id="ad2", name="Второе объявление")]
    _send_live_telegram_report(paused_details=details, analyzed=10, trigger="manual", errors=[])

    _send_telegram.assert_not_called()
    mock_buttons.assert_not_called()


@patch("services.telegram_bot.send_with_buttons", return_value=True)
@patch("services.notifications.send_critical_alert")
@patch("services.notifications.send_telegram")
def test_live_report_silent_when_only_held(_send_telegram, _send_alert, mock_buttons):
    """Только удержания, без ошибок → тоже тишина.

    Раньше на этот случай уходил отдельный отчёт «Держу N». Удержание — это
    отложенное решение, а не событие: оно подождёт утра вместе с остальным.
    """
    _send_live_telegram_report(
        paused_details=[], analyzed=10, trigger="cron", errors=[], held=[_held()]
    )

    _send_telegram.assert_not_called()
    mock_buttons.assert_not_called()


@patch("services.telegram_bot.send_with_buttons", return_value=True)
@patch("services.notifications.send_critical_alert")
@patch("services.notifications.send_telegram")
def test_live_report_errors_message_carries_held_section(
    _send_telegram, _send_alert, mock_buttons
):
    """Ошибки + удержания → одно сообщение: и «Ошибки», и «Держу».

    Это единственное, что слот шлёт днём, поэтому оно обязано быть полным:
    по одной ошибке владелец должен понимать и что заблокировано, и что при
    этом стоит под удержанием.
    """
    _send_live_telegram_report(
        paused_details=[],
        analyzed=10,
        trigger="cron",
        errors=["pause_guard ad1: inventory_incomplete"],
        held=[_held()],
    )

    _send_telegram.assert_called_once()
    msg_text = _send_telegram.call_args[0][0]
    assert "Ошибки (1)" in msg_text
    assert "Держу 1" in msg_text
    assert "Удержанная реклама" in msg_text
    assert len(msg_text) <= _TELEGRAM_MAX_LEN


# ---------------------------------------------------------------------------
# Антиспам: предложения уже ждут решения — отчёт не повторяем
# (жалоба владельца: «опять вот это пришло» — тот же список каждые 15 мин)
# ---------------------------------------------------------------------------

@patch("services.telegram_bot.send_with_buttons", return_value=True)
@patch("services.notifications.send_critical_alert")
@patch("services.notifications.send_telegram")
def test_live_report_silent_when_all_candidates_already_pending(
    _send_telegram, _send_alert, mock_buttons
):
    """Все кандидаты уже имеют висящее предложение → молчим.

    Раньше пустой список пауз давал «✅ аутсайдеров нет», что было ложью:
    аутсайдеры есть, просто их предложения ждут решения владельца.
    """
    _send_live_telegram_report(
        paused_details=[], analyzed=42, trigger="cron", errors=[], already_pending=7
    )

    _send_telegram.assert_not_called()
    mock_buttons.assert_not_called()


@patch("services.telegram_bot.send_with_buttons", return_value=True)
@patch("services.notifications.send_critical_alert")
@patch("services.notifications.send_telegram")
def test_live_report_silent_when_nothing_to_report(
    _send_telegram, _send_alert, mock_buttons
):
    """Кандидатов не было вовсе → тоже молчим.

    «✅ аутсайдеров нет — проанализировано 42» каждые 2 часа это ровно тот
    шум, из-за которого перестают читать и настоящие сообщения (решение
    владельца). Отсутствие новостей — не новость.
    """
    _send_live_telegram_report(
        paused_details=[], analyzed=42, trigger="cron", errors=[], already_pending=0
    )

    _send_telegram.assert_not_called()
    mock_buttons.assert_not_called()


@patch("services.telegram_bot.send_with_buttons", return_value=True)
@patch("services.notifications.send_critical_alert")
@patch("services.notifications.send_telegram")
def test_live_report_errors_break_silence_even_with_pending(
    _send_telegram, _send_alert, mock_buttons
):
    """Ошибки прогона доходят до владельца даже при ждущих предложениях."""
    _send_live_telegram_report(
        paused_details=[],
        analyzed=42,
        trigger="cron",
        errors=["pause_guard ad1: inventory_incomplete"],
        already_pending=3,
    )

    _send_telegram.assert_called_once()
    assert "Ошибки" in _send_telegram.call_args[0][0]
