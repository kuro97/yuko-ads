"""
Тесты форматирования Telegram-отчёта об автопаузах (services/autopilot.py:_format_pause_report).

Без сети — чистая функция форматирования строк.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта (чужой незакоммиченный код)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from services.autopilot import _format_pause_report, _TELEGRAM_MAX_LEN


def _ad(**overrides) -> dict:
    """Тестовое объявление для отчёта о паузе (все поля из реального ad-словаря)."""
    base = {
        "id": "ad1",
        "name": "Тестовое объявление",
        "city": "CityA",
        "spend": 100.0,
        "leads": 10,
        "cpl": 10.0,
        "qual_pct": 20.0,
        "payments": 1,
        "reason": "ROMI 88.8% < 200% — внизу ранга",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Базовый формат
# ---------------------------------------------------------------------------

def test_two_ads_have_blank_line_between_blocks():
    """Между блоками двух объявлений — пустая строка (читаемость)."""
    ads = [_ad(id="ad1", name="Первое"), _ad(id="ad2", name="Второе")]
    text = _format_pause_report(ads)

    assert "\n\n" in text
    # Блок первого объявления должен заканчиваться перед пустой строкой,
    # за которой начинается второй пронумерованный блок.
    assert "1. <b>CityA</b>" in text
    assert "2. <b>CityA</b>" in text
    idx1 = text.index("1. <b>CityA</b>")
    idx2 = text.index("2. <b>CityA</b>")
    between = text[idx1:idx2]
    assert "\n\n" in between


def test_report_has_numbering_and_city_bold():
    """Нумерация блоков + город жирным (<b>)."""
    ads = [_ad(id="ad1", city="CityB")]
    text = _format_pause_report(ads)

    assert "1. <b>CityB</b>" in text


def test_qual_and_payments_always_present():
    """Квал и оплаты обязательны в каждом блоке."""
    ads = [_ad(qual_pct=25.0, payments=2)]
    text = _format_pause_report(ads)

    assert "квал" in text
    assert "оплат" in text


def test_none_fields_render_as_dash_not_zero():
    """None-поля (нет данных из AMO) → «—», а не фиктивный 0."""
    ads = [_ad(qual_pct=None, payments=None)]
    text = _format_pause_report(ads)

    assert "квал —" in text
    assert "оплат —" in text
    # Явно НЕ должно быть "квал 0" — это была бы ложная информация
    assert "квал 0" not in text


def test_spend_rounded_to_integer_dollars():
    """Суммы округляются до целых $, тысячи разделены точкой (fmt_money,
    решение владельца: "1.500.000" вместо "1500000")."""
    ads = [_ad(spend=1253.67)]
    text = _format_pause_report(ads)

    assert "$1.254" in text  # округление до целого + разделитель тысяч точкой
    assert "1253.67" not in text


def test_long_name_truncated_to_60_chars():
    """Длинные названия обрезаются до ~60 символов."""
    long_name = "А" * 100
    ads = [_ad(name=long_name)]
    text = _format_pause_report(ads)

    # Полное 100-символьное имя не должно попасть в текст целиком
    assert long_name not in text


def test_reason_present_in_block():
    """Причина паузы — человеческой фразой в блоке."""
    ads = [_ad(reason="CPL $50 >= $40, 10 лидов")]
    text = _format_pause_report(ads)

    assert "CPL" in text


def test_no_text_footer_undo_replaced_by_buttons():
    """Текстового футера «дашборд → История решений» больше нет — вернуть
    объявление теперь можно inline-кнопкой «↩️ Вернуть» (см. tests/test_pause_undo.py),
    которая крепится к сообщению отдельно через send_with_buttons, а не текстом.
    Сообщение заканчивается последним блоком (строка причины с 📉), без футера."""
    ads = [_ad(reason="CPL 50 dollarov")]  # без спецсимволов HTML — избегаем html.escape в сравнении
    text = _format_pause_report(ads)

    assert "История решений" not in text
    assert "Вернуть любую" not in text
    # Последняя строка — часть блока (причина паузы с 📉), не футер
    last_line = text.strip().split("\n")[-1]
    assert last_line.startswith("   📉")
    assert last_line.endswith("CPL 50 dollarov")


def test_header_has_count():
    """Заголовок сообщения содержит число поставленных на паузу."""
    ads = [_ad(id="ad1"), _ad(id="ad2"), _ad(id="ad3")]
    text = _format_pause_report(ads)

    assert "3" in text.split("\n")[0]


# ---------------------------------------------------------------------------
# Усечение при превышении лимита Telegram (4096 символов)
# ---------------------------------------------------------------------------

def test_many_ads_truncated_with_and_more_suffix():
    """Много объявлений → текст не превышает 4096 символов, есть '…и ещё N'."""
    ads = [
        _ad(id=f"ad{i}", name=f"Объявление с довольно длинным названием номер {i} по теме страха PRODB")
        for i in range(80)
    ]
    text = _format_pause_report(ads)

    assert len(text) <= _TELEGRAM_MAX_LEN
    assert "и ещё" in text
    assert "дашборд" in text


def test_truncation_keeps_only_full_blocks():
    """При усечении в тексте остаются только целые блоки (без обрыва посередине)."""
    ads = [
        _ad(id=f"ad{i}", name=f"Объявление номер {i} с длинным описанием темы и аудитории для теста")
        for i in range(80)
    ]
    text = _format_pause_report(ads)

    # Каждый включённый блок должен содержать все три строки метрик
    included_part = text.split("…и ещё")[0]
    # Число полных блоков (по количеству вхождений "💸") должно совпадать
    # с числом заголовков блоков "N. <b>"
    assert included_part.count("💸") == included_part.count(". <b>")


def test_small_list_not_truncated():
    """Малое число объявлений — усечения нет, все блоки на месте."""
    ads = [_ad(id=f"ad{i}") for i in range(3)]
    text = _format_pause_report(ads)

    assert "и ещё" not in text
    assert text.count("💸") == 3


def test_single_ad_report_under_limit():
    """Отчёт с одним объявлением — компактный, далеко от лимита."""
    ads = [_ad()]
    text = _format_pause_report(ads)

    assert len(text) < 1000
