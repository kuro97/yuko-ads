"""
Тесты services/fact_sheet.py — парсинг реального docs/product/fact-sheet-acme.md,
кэш с инвалидацией по mtime, fail-closed без .md.

Мокаем только пути к .md/.json (через monkeypatch на модульные константы),
чтобы не трогать боевой data/fact_sheet.json. Реальный .md читаем как есть —
он часть репо и должен парситься без сети/БД.
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google.genai до импорта модулей проекта (паттерн из test_coverage_monitor.py)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

import services.fact_sheet as fs


# ---------------------------------------------------------------------------
# Парсинг реального .md
# ---------------------------------------------------------------------------

def test_build_fact_sheet_json_parses_real_md():
    """Реальный docs/product/fact-sheet-acme.md парсится в полный dict фактов."""
    fact_sheet = fs.build_fact_sheet_json()

    assert len(fact_sheet["offer"]) >= 7, "Оффер §2 должен содержать >=7 пунктов"
    assert len(fact_sheet["promo_claims"]) >= 3, "promo_claims §6 должны быть заполнены"
    assert len(fact_sheet["social_proof_claims"]) >= 1, "social_proof_claims §2 должны быть заполнены"


def test_build_fact_sheet_json_testimonial_mode_enabled():
    """§3 «Реальные отзывы» заполнен реальными отзывами → testimonial_mode_enabled=True,
    testimonials содержит хотя бы 2 записи (текущее состояние докумена после заполнения §3)."""
    fact_sheet = fs.build_fact_sheet_json()

    assert len(fact_sheet["testimonials"]) >= 2, "§3 заполнен — должно быть хотя бы 2 отзыва"
    assert fact_sheet["testimonial_mode_enabled"] is True, \
        "testimonial_mode_enabled должен быть True когда §3 заполнен реальными отзывами"


def test_build_fact_sheet_json_testimonial_mode_disabled_on_empty_section(tmp_path):
    """§3 «Реальные отзывы» пуст → testimonial_mode_enabled=False, testimonials=[].
    Проверяется на синтетической фикстуре с пустым §3 (не на .md из репо — он
    содержит образцы отзывов), чтобы сценарий «режим выключен» остался покрытым."""
    md_path = tmp_path / "fact-sheet-acme.md"
    _write_fixture_md(md_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs, "_MD_PATH", md_path)
        fact_sheet = fs.build_fact_sheet_json()

    assert fact_sheet["testimonials"] == [], "§3 пуст — testimonials должен быть пустым списком"
    assert fact_sheet["testimonial_mode_enabled"] is False, \
        "testimonial_mode_enabled должен быть False пока §3 пуст"


def test_build_fact_sheet_json_products_and_rules():
    """Продукты (PRODA/PRODB) и правила несовместимости распарсены."""
    fact_sheet = fs.build_fact_sheet_json()

    proda = fact_sheet["products"]["proda"]
    prodb = fact_sheet["products"]["prodb"]
    assert proda["packages"] == [2, 4], "Пакеты PRODA — пакет 2 и пакет 4 (§1)"
    assert proda["main_package"] == 4
    assert proda["bonus"] == "Старт+"
    assert prodb["packages"] == [6], "PRODB продаётся только пакетом 6 (§1)"
    assert prodb["services_count"] == 6
    assert prodb["meetings_count"] == 8

    rule_ids = [r["id"] for r in fact_sheet["incompatibility_rules"]]
    assert "package2_prodb" in rule_ids
    assert "package6_proda" in rule_ids


def test_build_fact_sheet_json_deadlines_and_numbers():
    """Дедлайны и разрешённые числа собраны в плоские списки."""
    fact_sheet = fs.build_fact_sheet_json()

    assert len(fact_sheet["deadlines"]) >= 1, "Должен быть хотя бы один дедлайн"
    assert "2026-04-15" in fact_sheet["deadlines"], "Весенняя акция PRODA до 15 апреля (§1) — дедлайн"
    assert "2026-05-29" in fact_sheet["deadlines"], "Основной сезон продаж PRODB до 29 мая (§1) — дедлайн"
    assert "2026-04-20" in fact_sheet["deadlines"], "Онбординг новых клиентов PRODA 20 апреля (§1) — дедлайн"
    assert "10" in fact_sheet["allowed_numbers"], "Менеджер ведёт не больше 10 клиентов (§2 оффера) — разрешённое число"
    assert "12" in fact_sheet["allowed_numbers"], "Рассрочка на 12 месяцев (§2 оффера) — разрешённое число"
    assert "6" in fact_sheet["allowed_numbers"], "Пакет 6 PRODB (§1) — разрешённое число"
    assert "8" in fact_sheet["allowed_numbers"], "8 встреч с экспертом за сезон PRODB (§1) — разрешённое число"
    assert fact_sheet["cities"] == ["CityA", "CityB", "CityC", "CityD", "CityE"]


def test_build_fact_sheet_json_missing_md_raises():
    """Отсутствие .md → FactSheetError (fail-closed, без .md генерация ТЗ запрещена)."""
    fake_md = Path("/nonexistent/path/fact-sheet-acme.md")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs, "_MD_PATH", fake_md)
        with pytest.raises(fs.FactSheetError):
            fs.build_fact_sheet_json()


def test_build_fact_sheet_json_empty_md_raises(tmp_path):
    """Пустой .md → FactSheetError."""
    empty_md = tmp_path / "empty.md"
    empty_md.write_text("", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs, "_MD_PATH", empty_md)
        with pytest.raises(fs.FactSheetError):
            fs.build_fact_sheet_json()


def test_build_fact_sheet_json_md_without_offer_section_raises(tmp_path):
    """.md без секции «2. Оффер» повреждён → FactSheetError."""
    broken_md = tmp_path / "broken.md"
    broken_md.write_text(
        "# Fact Sheet\n\n## 1. Продукты\n\n### PRODA\n- что-то\n",
        encoding="utf-8",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs, "_MD_PATH", broken_md)
        with pytest.raises(fs.FactSheetError):
            fs.build_fact_sheet_json()


# ---------------------------------------------------------------------------
# load_fact_sheet: кэш + инвалидация по mtime (всё на tmp_path, без сети/боевого json)
# ---------------------------------------------------------------------------

_MD_FIXTURE = """# Fact Sheet

## 1. Продукты

### PRODA
- Пакеты PRODA: пакет 2 и пакет 4 — основной.
- Бонус: «Старт+» (скидка на второй сезон).
- Сроки 2026: весенняя акция 1 марта – 15 апреля; онбординг новых клиентов 20 апреля.

### PRODB
- Пакет PRODB: только пакет 6, 6 услуг, 8 встреч; основной сезон продаж 15 января – 29 мая.

## 2. Оффер ACME

Базовый оффер:
- Бесплатная консультация.
- Один менеджер ведёт не больше 10 клиентов.
- Персональный менеджер.
- Пробный период перед оплатой.
- Рассрочка банка-партнёра на 12 месяцев.
- 4 услуги в одном месте.
- Весенняя акция на PRODA.

Одобренные claims (соц-доказательство):
- «Каждый третий новый клиент приходит в ACME по рекомендации».

## 3. Реальные отзывы

- <!-- пусто -->

## 4. Правила несовместимости

- «пакет 2 + PRODB» = ошибка.
- «пакет 6 + PRODA» = ошибка.

## 6. Типы рекламы и акценты

Примеры прямых реклам:
- Рассрочка банка-партнёра на 12 месяцев
- 4 услуги в одном месте
- Весенняя акция на PRODA
"""


def _write_fixture_md(path: Path) -> None:
    path.write_text(_MD_FIXTURE, encoding="utf-8")


def test_load_fact_sheet_builds_and_writes_cache(tmp_path):
    """При отсутствии json-кэша load_fact_sheet пересобирает из .md и пишет кэш на диск."""
    md_path = tmp_path / "fact-sheet-acme.md"
    json_path = tmp_path / "fact_sheet.json"
    _write_fixture_md(md_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs, "_MD_PATH", md_path)
        mp.setattr(fs, "_JSON_PATH", json_path)

        assert not json_path.exists()
        result = fs.load_fact_sheet()

    assert json_path.exists(), "load_fact_sheet должен записать кэш на диск"
    assert result["promo_claims"], "Результат должен содержать разобранные promo_claims"


def test_load_fact_sheet_uses_cache_when_md_not_changed(tmp_path):
    """Если .md не менялся после генерации кэша — используется кэш (не пересобирается)."""
    md_path = tmp_path / "fact-sheet-acme.md"
    json_path = tmp_path / "fact_sheet.json"
    _write_fixture_md(md_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs, "_MD_PATH", md_path)
        mp.setattr(fs, "_JSON_PATH", json_path)

        first = fs.load_fact_sheet()
        # Портим json "изнутри", чтобы отличить "из кэша" от "пересобрано":
        # если возвращённый вторым вызовом объект содержит маркер — значит кэш реально читался.
        import json as _json
        cached_raw = _json.loads(json_path.read_text(encoding="utf-8"))
        cached_raw["_from_cache_marker"] = True
        json_path.write_text(_json.dumps(cached_raw, ensure_ascii=False), encoding="utf-8")

        second = fs.load_fact_sheet()

    assert second.get("_from_cache_marker") is True, \
        "Второй вызов должен вернуть данные из кэша (mtime .md не менялся)"
    assert first["promo_claims"] == second["promo_claims"]


def test_load_fact_sheet_invalidates_cache_when_md_newer(tmp_path):
    """json старше .md по mtime → кэш пересобирается заново из .md."""
    md_path = tmp_path / "fact-sheet-acme.md"
    json_path = tmp_path / "fact_sheet.json"
    _write_fixture_md(md_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs, "_MD_PATH", md_path)
        mp.setattr(fs, "_JSON_PATH", json_path)

        fs.load_fact_sheet()

        import json as _json
        cached_raw = _json.loads(json_path.read_text(encoding="utf-8"))
        cached_raw["_from_cache_marker"] = True
        json_path.write_text(_json.dumps(cached_raw, ensure_ascii=False), encoding="utf-8")

        # Делаем .md "новее" кэша: двигаем mtime .md вперёд во времени
        future_time = time.time() + 10
        os.utime(md_path, (future_time, future_time))

        result = fs.load_fact_sheet()

    assert "_from_cache_marker" not in result, \
        "Кэш должен быть пересобран заново, когда .md новее записанного в кэш mtime"
    assert result["promo_claims"], "Пересобранный результат всё ещё содержит promo_claims"


def test_load_fact_sheet_force_rebuild_ignores_cache(tmp_path):
    """force_rebuild=True игнорирует валидный кэш и пересобирает из .md."""
    md_path = tmp_path / "fact-sheet-acme.md"
    json_path = tmp_path / "fact_sheet.json"
    _write_fixture_md(md_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs, "_MD_PATH", md_path)
        mp.setattr(fs, "_JSON_PATH", json_path)

        fs.load_fact_sheet()

        import json as _json
        cached_raw = _json.loads(json_path.read_text(encoding="utf-8"))
        cached_raw["_from_cache_marker"] = True
        json_path.write_text(_json.dumps(cached_raw, ensure_ascii=False), encoding="utf-8")

        result = fs.load_fact_sheet(force_rebuild=True)

    assert "_from_cache_marker" not in result, \
        "force_rebuild=True должен игнорировать кэш даже если .md не менялся"


def test_load_fact_sheet_missing_md_raises_fail_closed(tmp_path):
    """Нет .md и нет валидного кэша → FactSheetError (fail-closed)."""
    md_path = tmp_path / "does-not-exist.md"
    json_path = tmp_path / "fact_sheet.json"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs, "_MD_PATH", md_path)
        mp.setattr(fs, "_JSON_PATH", json_path)

        with pytest.raises(fs.FactSheetError):
            fs.load_fact_sheet()


# ---------------------------------------------------------------------------
# list_allowed_numbers / fact_sheet_for_prompt
# ---------------------------------------------------------------------------

def test_list_allowed_numbers_returns_flat_list():
    """list_allowed_numbers возвращает плоский список чисел из fact_sheet."""
    fact_sheet = fs.build_fact_sheet_json()
    numbers = fs.list_allowed_numbers(fact_sheet)

    assert isinstance(numbers, list)
    assert "10" in numbers
    assert "12" in numbers


def test_fact_sheet_for_prompt_brand_voice_excludes_testimonials():
    """voice='brand' не должен включать блок отзывов, даже если testimonials есть."""
    fact_sheet = fs.build_fact_sheet_json()
    prompt_text = fs.fact_sheet_for_prompt(fact_sheet, voice="brand")

    assert isinstance(prompt_text, str)
    assert len(prompt_text) > 0
    assert "РЕАЛЬНЫЕ ОТЗЫВЫ" not in prompt_text, \
        "voice=brand не должен инъецировать блок отзывов"


def test_fact_sheet_for_prompt_testimonial_voice_disabled_mode_excludes_block(tmp_path):
    """voice='testimonial', но testimonial_mode_enabled=False → блок отзывов не включается.
    Проверяется на синтетической фикстуре с пустым §3 (.md из репо содержит
    образцы отзывов), чтобы сценарий «режим выключен» остался покрытым."""
    md_path = tmp_path / "fact-sheet-acme.md"
    _write_fixture_md(md_path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fs, "_MD_PATH", md_path)
        fact_sheet = fs.build_fact_sheet_json()

    assert fact_sheet["testimonial_mode_enabled"] is False

    prompt_text = fs.fact_sheet_for_prompt(fact_sheet, voice="testimonial")

    assert "РЕАЛЬНЫЕ ОТЗЫВЫ" not in prompt_text, \
        "Блок отзывов не должен появляться, пока режим отключён"


def test_fact_sheet_for_prompt_testimonial_voice_enabled_mode_includes_block():
    """voice='testimonial' и testimonial_mode_enabled=True (реальный Fact Sheet, §3 заполнен)
    → блок отзывов включается в промпт."""
    fact_sheet = fs.build_fact_sheet_json()
    assert fact_sheet["testimonial_mode_enabled"] is True

    prompt_text = fs.fact_sheet_for_prompt(fact_sheet, voice="testimonial")

    assert "РЕАЛЬНЫЕ ОТЗЫВЫ" in prompt_text, \
        "Блок отзывов должен появляться, когда режим включён и voice=testimonial"
