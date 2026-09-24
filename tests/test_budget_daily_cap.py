"""
Тесты services/budget_daily_cap.py — дневной кап подъёмов бюджета per-adset.

Проверяем (см. docs/specs/ARCH-phase2-budget-pilot.md §9, задача T2):
- накопление подъёмов до 15% за календарный день CityA
- исчерпание дневного капа → remaining == 0
- независимость нескольких адсетов
- rollover при смене календарного дня CityA (счётчики сброшены, start_budget пересчитан)
- start_budget фиксируется один раз в день (не плывёт от текущего бюджета)
- start_budget <= 0 → remaining 0 (защита от деления на ноль)
- битый/отсутствующий state-файл не роняет чтение
- атомарность записи (нет .tmp файла после операций)
- процент считается ОТ start_budget, а не от текущего бюджета

State-файл во всех тестах перенаправлен в tmp_path через monkeypatch,
реальный диск проекта не трогаем. Без сети.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import budget_daily_cap


_TZ_LOCAL = timezone(timedelta(hours=5))


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Подменяет путь к state-файлу дневного капа на временный каталог."""
    state_file = tmp_path / "budget_daily_cap_state.json"
    monkeypatch.setattr(budget_daily_cap, "_CAP_STATE_FILE", state_file)
    return state_file


def _now(date_str: str = "2026-07-02", hour: int = 10) -> datetime:
    """Вспомогательная функция: datetime в TZ CityA на заданную дату/час."""
    return datetime.fromisoformat(f"{date_str}T{hour:02d}:00:00").replace(tzinfo=_TZ_LOCAL)


# ---------------------------------------------------------------------------
# Накопление подъёмов до 15%
# ---------------------------------------------------------------------------

class TestНакоплениеПодъёмов:
    def test_первый_подъём_фиксирует_start_и_остаток(self, isolated_state):
        """Первое обращение: start=$100, remaining == daily_cap_pct (весь лимит доступен)."""
        now = _now()
        remaining = budget_daily_cap.get_remaining_daily_pct("adset1", 100.0, 15.0, now=now)
        assert remaining == 15.0

    def test_подъём_на_10_процентов_оставляет_5(self, isolated_state):
        """start=$100: +10% → remaining 5%."""
        now = _now()
        # Первое обращение фиксирует start_budget=100
        budget_daily_cap.get_remaining_daily_pct("adset1", 100.0, 15.0, now=now)
        # Реальный подъём: 100 -> 110 (+10% от start)
        budget_daily_cap.record_raise("adset1", 100.0, 110.0, now=now)

        remaining = budget_daily_cap.get_remaining_daily_pct("adset1", 110.0, 15.0, now=now)
        assert remaining == pytest.approx(5.0)

    def test_второй_подъём_5_процентов_остаток_0(self, isolated_state):
        """start=$100: +10% → remaining 5 → +5% → remaining 0 (см. спека §10.1 пример)."""
        now = _now()
        budget_daily_cap.get_remaining_daily_pct("adset1", 100.0, 15.0, now=now)
        budget_daily_cap.record_raise("adset1", 100.0, 110.0, now=now)  # +10%

        remaining_after_1 = budget_daily_cap.get_remaining_daily_pct("adset1", 110.0, 15.0, now=now)
        assert remaining_after_1 == pytest.approx(5.0)

        # Второй подъём: effective=min(15,5)=5% от start=100 -> +$5 -> 115
        budget_daily_cap.record_raise("adset1", 110.0, 115.0, now=now)

        remaining_after_2 = budget_daily_cap.get_remaining_daily_pct("adset1", 115.0, 15.0, now=now)
        assert remaining_after_2 == pytest.approx(0.0)

    def test_суммарный_подъём_не_превышает_15_процентов(self, isolated_state):
        """Итоговый бюджет после двух подъёмов не выше start*1.15 = $115."""
        now = _now()
        budget_daily_cap.get_remaining_daily_pct("adset1", 100.0, 15.0, now=now)
        budget_daily_cap.record_raise("adset1", 100.0, 110.0, now=now)
        budget_daily_cap.record_raise("adset1", 110.0, 115.0, now=now)

        state = json.loads(isolated_state.read_text(encoding="utf-8"))
        raised_pct = state["adsets"]["adset1"]["raised_pct"]
        assert raised_pct == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Исчерпание капа
# ---------------------------------------------------------------------------

class TestИсчерпаниеКапа:
    def test_кап_исчерпан_remaining_равен_нулю(self, isolated_state):
        """Адсет уже поднят на +15% сегодня → следующий прогон remaining == 0."""
        now = _now()
        budget_daily_cap.get_remaining_daily_pct("adset1", 100.0, 15.0, now=now)
        budget_daily_cap.record_raise("adset1", 100.0, 115.0, now=now)  # +15% разом

        remaining = budget_daily_cap.get_remaining_daily_pct("adset1", 115.0, 15.0, now=now)
        assert remaining == 0.0

    def test_кап_исчерпан_не_уходит_в_минус(self, isolated_state):
        """Если raised_pct случайно превысил cap (напр. ручной подъём) — remaining всё равно 0, не отрицательный."""
        now = _now()
        budget_daily_cap.get_remaining_daily_pct("adset1", 100.0, 15.0, now=now)
        budget_daily_cap.record_raise("adset1", 100.0, 130.0, now=now)  # +30% (гипотетически больше капа)

        remaining = budget_daily_cap.get_remaining_daily_pct("adset1", 130.0, 15.0, now=now)
        assert remaining == 0.0


# ---------------------------------------------------------------------------
# Независимость нескольких адсетов
# ---------------------------------------------------------------------------

class TestНезависимостьАдсетов:
    def test_несколько_адсетов_независимы(self, isolated_state):
        """Подъём одного адсета не влияет на остаток другого."""
        now = _now()

        budget_daily_cap.get_remaining_daily_pct("adset1", 100.0, 15.0, now=now)
        budget_daily_cap.get_remaining_daily_pct("adset2", 200.0, 15.0, now=now)

        budget_daily_cap.record_raise("adset1", 100.0, 115.0, now=now)  # адсет1 исчерпан

        remaining_1 = budget_daily_cap.get_remaining_daily_pct("adset1", 115.0, 15.0, now=now)
        remaining_2 = budget_daily_cap.get_remaining_daily_pct("adset2", 200.0, 15.0, now=now)

        assert remaining_1 == 0.0
        assert remaining_2 == 15.0

    def test_start_budget_разных_адсетов_не_смешиваются(self, isolated_state):
        """start_budget адсета1 не влияет на start_budget адсета2."""
        now = _now()

        start_1 = budget_daily_cap.get_day_start_budget("adset1", 100.0, now=now)
        start_2 = budget_daily_cap.get_day_start_budget("adset2", 250.0, now=now)

        assert start_1 == 100.0
        assert start_2 == 250.0


# ---------------------------------------------------------------------------
# Rollover при смене календарного дня CityA
# ---------------------------------------------------------------------------

class TestRolloverДня:
    def test_переход_дня_сбрасывает_счётчики(self, isolated_state):
        """Адсет поднят +15% вчера → сегодня remaining снова 15%."""
        yesterday = _now("2026-07-01")
        today = _now("2026-07-02")

        budget_daily_cap.get_remaining_daily_pct("adset1", 100.0, 15.0, now=yesterday)
        budget_daily_cap.record_raise("adset1", 100.0, 115.0, now=yesterday)

        remaining_yesterday = budget_daily_cap.get_remaining_daily_pct("adset1", 115.0, 15.0, now=yesterday)
        assert remaining_yesterday == 0.0

        remaining_today = budget_daily_cap.get_remaining_daily_pct("adset1", 115.0, 15.0, now=today)
        assert remaining_today == 15.0

    def test_переход_дня_пересчитывает_start_budget(self, isolated_state):
        """При новом дне start_budget фиксируется заново от ТЕКУЩЕГО бюджета (не от вчерашнего)."""
        yesterday = _now("2026-07-01")
        today = _now("2026-07-02")

        budget_daily_cap.get_day_start_budget("adset1", 100.0, now=yesterday)
        budget_daily_cap.record_raise("adset1", 100.0, 115.0, now=yesterday)

        # Сегодня текущий бюджет уже $115 (вчерашний подъём применился в FB)
        start_today = budget_daily_cap.get_day_start_budget("adset1", 115.0, now=today)
        assert start_today == 115.0

    def test_rollover_очищает_все_адсеты(self, isolated_state):
        """При смене дня сбрасываются счётчики ВСЕХ адсетов, не только запрошенного."""
        yesterday = _now("2026-07-01")
        today = _now("2026-07-02")

        budget_daily_cap.get_remaining_daily_pct("adset1", 100.0, 15.0, now=yesterday)
        budget_daily_cap.record_raise("adset1", 100.0, 115.0, now=yesterday)
        budget_daily_cap.get_remaining_daily_pct("adset2", 200.0, 15.0, now=yesterday)
        budget_daily_cap.record_raise("adset2", 200.0, 230.0, now=yesterday)

        # Обращение к adset2 сегодня триггерит rollover для ВСЕХ адсетов сразу
        remaining_2_today = budget_daily_cap.get_remaining_daily_pct("adset2", 230.0, 15.0, now=today)
        assert remaining_2_today == 15.0

        remaining_1_today = budget_daily_cap.get_remaining_daily_pct("adset1", 115.0, 15.0, now=today)
        assert remaining_1_today == 15.0


# ---------------------------------------------------------------------------
# start_budget фиксируется один раз в день
# ---------------------------------------------------------------------------

class TestStartBudgetФиксация:
    def test_start_budget_не_плывёт_от_текущего_бюджета(self, isolated_state):
        """Два обращения за день с разными current → оба раза вернётся значение ПЕРВОГО обращения."""
        now = _now()

        first = budget_daily_cap.get_day_start_budget("adset1", 100.0, now=now)
        # Бюджет в FB изменился (например, вручную), но start дня должен остаться прежним
        second = budget_daily_cap.get_day_start_budget("adset1", 999.0, now=now)

        assert first == 100.0
        assert second == 100.0

    def test_get_remaining_не_переопределяет_start_budget(self, isolated_state):
        """get_remaining_daily_pct с новым current не должен менять уже зафиксированный start_budget."""
        now = _now()

        budget_daily_cap.get_day_start_budget("adset1", 100.0, now=now)
        budget_daily_cap.get_remaining_daily_pct("adset1", 500.0, 15.0, now=now)

        start_after = budget_daily_cap.get_day_start_budget("adset1", 777.0, now=now)
        assert start_after == 100.0


# ---------------------------------------------------------------------------
# start_budget <= 0 → защита от деления на ноль
# ---------------------------------------------------------------------------

class TestStartБюджетНольИлиМеньше:
    def test_start_budget_ноль_remaining_ноль(self, isolated_state):
        """current_budget_usd == 0 (адсет без бюджета/CBO) → remaining 0.0, не падает."""
        now = _now()
        remaining = budget_daily_cap.get_remaining_daily_pct("adset1", 0.0, 15.0, now=now)
        assert remaining == 0.0

    def test_start_budget_отрицательный_remaining_ноль(self, isolated_state):
        """Отрицательный бюджет (некорректные данные) → remaining 0.0, не делится на ноль/не падает."""
        now = _now()
        remaining = budget_daily_cap.get_remaining_daily_pct("adset1", -50.0, 15.0, now=now)
        assert remaining == 0.0

    def test_record_raise_start_ноль_не_падает(self, isolated_state):
        """record_raise при start_budget<=0 не должен бросать исключение (просто не учитывает %)."""
        now = _now()
        budget_daily_cap.get_day_start_budget("adset1", 0.0, now=now)
        # Не должно упасть с ZeroDivisionError
        budget_daily_cap.record_raise("adset1", 0.0, 10.0, now=now)

        state = json.loads(isolated_state.read_text(encoding="utf-8"))
        assert state["adsets"]["adset1"]["raised_pct"] == 0.0


# ---------------------------------------------------------------------------
# Битый/отсутствующий state не роняет чтение
# ---------------------------------------------------------------------------

class TestБитыйState:
    def test_отсутствующий_файл_не_роняет(self, isolated_state):
        """Файла ещё нет на диске → _load_cap_state возвращает дефолт, не падает."""
        assert not isolated_state.exists()
        state = budget_daily_cap._load_cap_state()
        assert state == {"day": None, "adsets": {}}

    def test_битый_json_не_роняет(self, isolated_state):
        """Файл с невалидным JSON → возвращаем дефолт, не бросаем исключение."""
        isolated_state.parent.mkdir(parents=True, exist_ok=True)
        isolated_state.write_text("{не json вообще", encoding="utf-8")

        state = budget_daily_cap._load_cap_state()
        assert state == {"day": None, "adsets": {}}

    def test_битый_state_после_него_можно_нормально_работать(self, isolated_state):
        """После битого файла get_remaining_daily_pct продолжает работать корректно."""
        isolated_state.parent.mkdir(parents=True, exist_ok=True)
        isolated_state.write_text("{полная ерунда", encoding="utf-8")

        remaining = budget_daily_cap.get_remaining_daily_pct("adset1", 100.0, 15.0, now=_now())
        assert remaining == 15.0

    def test_adsets_не_dict_в_файле_не_роняет(self, isolated_state):
        """Поле adsets битого типа (не dict) → заменяется на пустой dict."""
        isolated_state.parent.mkdir(parents=True, exist_ok=True)
        isolated_state.write_text(
            json.dumps({"day": "2026-07-02", "adsets": "not a dict"}), encoding="utf-8"
        )

        state = budget_daily_cap._load_cap_state()
        assert state["adsets"] == {}


# ---------------------------------------------------------------------------
# Атомарность записи
# ---------------------------------------------------------------------------

class TestАтомарностьЗаписи:
    def test_нет_tmp_файла_после_get_day_start_budget(self, isolated_state):
        """После фиксации start_budget .tmp-файл не остаётся на диске."""
        budget_daily_cap.get_day_start_budget("adset1", 100.0, now=_now())
        tmp_file = isolated_state.with_suffix(".json.tmp")
        assert not tmp_file.exists()
        assert isolated_state.exists()

    def test_нет_tmp_файла_после_record_raise(self, isolated_state):
        """После record_raise .tmp-файл не остаётся на диске."""
        now = _now()
        budget_daily_cap.get_day_start_budget("adset1", 100.0, now=now)
        budget_daily_cap.record_raise("adset1", 100.0, 110.0, now=now)

        tmp_file = isolated_state.with_suffix(".json.tmp")
        assert not tmp_file.exists()

    def test_нет_tmp_файла_после_rollover(self, isolated_state):
        """После смены дня и обращения — .tmp-файл не остаётся."""
        yesterday = _now("2026-07-01")
        today = _now("2026-07-02")
        budget_daily_cap.get_remaining_daily_pct("adset1", 100.0, 15.0, now=yesterday)
        budget_daily_cap.get_remaining_daily_pct("adset1", 115.0, 15.0, now=today)

        tmp_file = isolated_state.with_suffix(".json.tmp")
        assert not tmp_file.exists()

    def test_файл_содержит_валидный_json_после_операций(self, isolated_state):
        """Финальный файл всегда читаемый JSON (не оборван на середине записи)."""
        now = _now()
        budget_daily_cap.get_day_start_budget("adset1", 100.0, now=now)
        budget_daily_cap.record_raise("adset1", 100.0, 110.0, now=now)

        content = isolated_state.read_text(encoding="utf-8")
        parsed = json.loads(content)  # не должно бросить исключение
        assert parsed["day"] == "2026-07-02"


# ---------------------------------------------------------------------------
# Процент считается ОТ start_budget, а не от текущего бюджета
# ---------------------------------------------------------------------------

class TestПроцентОтStartБюджета:
    def test_added_pct_считается_от_start_а_не_от_current(self, isolated_state):
        """Пример из спеки §10.1: start=$100, подъём2 +$5 (5% от start=100), а НЕ 5% от current=110 (=$5.5)."""
        now = _now()
        budget_daily_cap.get_day_start_budget("adset1", 100.0, now=now)
        budget_daily_cap.record_raise("adset1", 100.0, 110.0, now=now)  # +10% от 100

        # Подъём на такую же абсолютную сумму $5, но current уже 110
        budget_daily_cap.record_raise("adset1", 110.0, 115.0, now=now)

        state = json.loads(isolated_state.read_text(encoding="utf-8"))
        # (110-100)/100*100 + (115-110)/100*100 = 10 + 5 = 15, НЕ (115-100)/100*100=15 случайно совпадает,
        # но проверяем именно что raised_pct = 15, а не пересчитан по сложному проценту от current (что дало бы иначе)
        assert state["adsets"]["adset1"]["raised_pct"] == pytest.approx(15.0)

    def test_add_pct_не_зависит_от_текущего_бюджета_если_он_вырос_вручную(self, isolated_state):
        """Если current вырос сильнее (ручное вмешательство), % всё равно считается от start."""
        now = _now()
        budget_daily_cap.get_day_start_budget("adset1", 100.0, now=now)
        # current вручную стал 150, подняли до 160 (+$10, т.е. +10% от start=100)
        budget_daily_cap.record_raise("adset1", 150.0, 160.0, now=now)

        state = json.loads(isolated_state.read_text(encoding="utf-8"))
        assert state["adsets"]["adset1"]["raised_pct"] == pytest.approx(10.0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
