"""
Тесты services/formatting.py — общие форматтеры для Telegram-отчётов.

fmt_money уже покрыт косвенно через test_pause_report.py/test_auto_launch_report.py,
здесь фокус на truncate_at_word_boundary — общей утилите обрезки текста по
границе слова (переиспользуется в autopilot.py для причин пауз и в
auto_launch.py для подписей кнопок «⏸ Остановить»).
"""

from services.formatting import truncate_at_word_boundary


class TestTruncateAtWordBoundary:
    def test_короткий_текст_не_обрезается(self):
        assert truncate_at_word_boundary("Бонус", 22) == "Бонус"

    def test_текст_ровно_на_границе_лимита_не_обрезается(self):
        text = "А" * 22
        assert truncate_at_word_boundary(text, 22) == text

    def test_длинный_текст_режется_по_последнему_пробелу(self):
        text = "Петров / Тестовая подтема"
        assert truncate_at_word_boundary(text, 22) == "Петров / Тестовая…"

    def test_обрубка_посреди_слова_запрещена(self):
        """Регрессия бага: name[:22] раньше давал «Петров / Тестовая подт» —
        обрубок посреди слова «подтема». Владелец против таких обрубков."""
        text = "Петров / Тестовая подтема"
        result = truncate_at_word_boundary(text, 22)

        assert "подт" not in result
        assert result.endswith("…")

    def test_без_пробелов_режет_жёстко_страховка(self):
        """Если пробела нет вообще (одно длинное «слово») — режем жёстко,
        чтобы не выдать бесконечно длинную строку."""
        text = "А" * 100
        result = truncate_at_word_boundary(text, 22)

        assert result == "А" * 22 + "…"

    def test_многоточие_добавляется_только_при_обрезке(self):
        assert "…" not in truncate_at_word_boundary("Коротко", 22)
