"""
Чтение недельного плана расходов из Google Sheets.

Экспортирует план в формате xlsx через Google Sheets export API,
парсит через openpyxl. Результат кешируется на диск (TTL 24ч).

Ожидаемый формат листа: ПО ВКЛАДКЕ НА ГОРОД (имена — из реестра
_CITY_SHEET_NAMES), у каждого города свои бюджеты. read_general_plan()
читает ВСЕ городские вкладки по ИМЕНИ и суммирует их бюджеты/выручку —
иначе план-гейт сравнивал бы план одного города с расходом всего кабинета
(ложная блокировка).

Упрощённый формат — единая вкладка с A1=="GENERAL" — остаётся
fallback-путём: если ни одна вкладка не найдена по имени из реестра
городов, ищем по маркеру A1 (см. _fetch_plan_legacy).

Структура каждой вкладки (0-индекс колонок):
  A(0)=label, B(1)=план(месяц), E(4)=неделя1-7, H(7)=неделя8-14,
  K(10)=неделя15-21, N(13)=неделя22-28, Q(16)=неделя29-31.

Строки (по label в колонке A):
  «Факт»        — выручка-план (¤)
  «Бюджет тотал» — НЕОБЯЗАТЕЛЬНАЯ строка итогового бюджета вкладки. Если она
                есть, остальные строки «Бюджет» считаются разбивкой (их нельзя
                суммировать напрямую), и итог берётся из «Бюджет тотал»
                ВМЕСТО «Бюджет».
  «Бюджет»    — итог бюджета на вкладках без «Бюджет тотал». ВНИМАНИЕ: на
                вкладке может быть несколько строк «Бюджет», часть из них —
                битые агрегатные (месячный план B=0/#REF!, бывает и с числовыми
                нулями по неделям — не отсеивается проверкой на число!), одна —
                рабочая. Выбор: из всех строк «Бюджет» берём ту, у которой
                месячный план (колонка B, индекс 1) — МАКСИМАЛЬНОЕ
                ПОЛОЖИТЕЛЬНОЕ число (битые строки отсеиваются, у них B=0 или
                #REF!). Если ни у одной строки нет валидного B>0 (колонка B
                недоступна) — fallback на первую строку с числовым недельным
                значением. Если и такой нет — бюджет не определён (None → эта
                вкладка исключается).
  «% от факта» — ДРР target (доля, 0.08 = 8%).

Вкладки вне реестра городов (например, план отдельного канала или кабинета)
в сумму основного гейта НЕ включаются.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Локальное время (UTC+5 по умолчанию, настраивается этой константой)
_TZ_LOCAL = timezone(timedelta(hours=5))

# TTL диск-кеша — 24 часа
_CACHE_TTL_SEC = 24 * 3600

# Путь к кеш-файлу
_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "plan_cache.json"

# Реестр городских вкладок. Матч по имени вкладки после strip() — в имени
# вкладки может оказаться хвостовой пробел («CityA »). A1-маркер НЕ критерий
# отбора этих вкладок (A1 может быть пустым или не "GENERAL").
_CITY_SHEET_NAMES = ["CityA", "CityB", "CityC", "CityD", "CityE"]

# Недельные окна по дню месяца: (день_от, день_до_включительно, номер_недели, колонка_индекс)
_WEEK_WINDOWS = [
    (1,  7,  1, 4),   # E(4)  = 1-7
    (8,  14, 2, 7),   # H(7)  = 8-14
    (15, 21, 3, 10),  # K(10) = 15-21
    (22, 28, 4, 13),  # N(13) = 22-28
    (29, 31, 5, 16),  # Q(16) = 29-31
]


def _get_week_col(day_of_month: int) -> tuple[int, int, int]:
    """Определяет номер недели и индекс колонки по дню месяца.

    Returns:
        (week_num, col_idx, day_from) — номер недели (1-5), индекс колонки, первый день окна.
    """
    for day_from, day_to, week_num, col_idx in _WEEK_WINDOWS:
        if day_from <= day_of_month <= day_to:
            return week_num, col_idx, day_from
    # Фолбэк: последнее окно (29-31)
    return 5, 16, 29


def _is_numeric(value) -> bool:
    """Возвращает True если значение — настоящее число (не строка, не None, не ошибка Excel)."""
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    # Строки типа '#REF!', '#N/A', '#VALUE!' и т.п. — не числа
    return False


def _load_cache() -> dict:
    """Загружает кеш из диска. При ошибке — возвращает пустой dict."""
    if not _CACHE_FILE.exists():
        return {}
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("plan_reader: ошибка чтения кеша — %s", exc)
        return {}


def _save_cache(data: dict) -> None:
    """Атомарно сохраняет кеш на диск (tmp → rename)."""
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_CACHE_FILE)
    except Exception as exc:
        logger.error("plan_reader: не удалось сохранить кеш — %s", exc)


def _cache_key(sheet_id: str, now: datetime) -> str:
    """Ключ кеша: sheet_id + год-месяц + номер недели.

    Каждая неделя кешируется отдельно — бюджет и выручка-план разные
    по неделям одного месяца. Макс 5 обращений к Google в месяц.
    """
    week_num, _, _ = _get_week_col(now.day)
    return f"{sheet_id}:{now.year}-{now.month:02d}:w{week_num}"


def _parse_sheet_plan(sheet, col_idx: int) -> dict | None:
    """Парсит план (бюджет/выручка/юнитка) из ОДНОЙ вкладки листа по колонке недели.

    Args:
        sheet: openpyxl worksheet.
        col_idx: индекс колонки текущей недели (0-based).

    Returns:
        {budget_usd, revenue_plan_lcy, unit_target} или None, если строка
        «Бюджет»/«Бюджет тотал» с числовым значением не найдена (вкладка
        битая/пустая).
    """
    budget_usd: float | None = None
    revenue_plan_lcy: float | None = None
    unit_target: float | None = None

    # На вкладке может быть отдельная ИТОГОВАЯ строка «Бюджет тотал» —
    # тогда это единственная корректная сумма по городу (остальные строки
    # «Бюджет» на такой вкладке — разбивка, суммировать их напрямую нельзя).
    # На вкладках без «Бюджет тотал» используем обычную строку «Бюджет» —
    # там она итоговая.
    budget_total_candidates: list[tuple[float | None, float]] = []

    # Кандидаты строк «Бюджет»: (месячный_план_B|None, недельное_значение).
    # На вкладке бывает несколько строк «Бюджет» — верхняя(-ие) битая-агрегатная
    # (может содержать #REF! ИЛИ числовые нули), нижняя — рабочая с
    # фактическими цифрами. Выбираем строку с максимальным положительным месячным планом B —
    # у битых строк B тоже 0/#REF, у рабочей строки B > 0. Если ни у одного
    # кандидата нет валидного B (лист/тест не содержит колонку B) — fallback
    # на первую строку с числовым недельным значением.
    budget_candidates: list[tuple[float | None, float]] = []

    for row in sheet.iter_rows(values_only=True):
        if not row or row[0] is None:
            continue

        label_raw = str(row[0]).strip()

        # Выручка-план (¤) — строка «Факт»
        if label_raw == "Факт" and revenue_plan_lcy is None:
            val = row[col_idx] if len(row) > col_idx else None
            if _is_numeric(val):
                revenue_plan_lcy = float(val)
                logger.debug("plan_reader: выручка-план = %.0f ¤ (строка «Факт», кол.%d)", revenue_plan_lcy, col_idx)

        # Итоговый бюджет ($) — строка «Бюджет тотал» (приоритетнее «Бюджет»).
        elif label_raw == "Бюджет тотал":
            week_val = row[col_idx] if len(row) > col_idx else None
            month_val_raw = row[1] if len(row) > 1 else None
            month_val = float(month_val_raw) if _is_numeric(month_val_raw) else None
            if _is_numeric(week_val):
                budget_total_candidates.append((month_val, float(week_val)))
                logger.debug(
                    "plan_reader: кандидат «Бюджет тотал» — месяц(B)=%s, неделя(кол.%d)=%.2f",
                    month_val, col_idx, float(week_val),
                )

        # Бюджет ($) — строка «Бюджет». Может встречаться несколько раз —
        # копим кандидатов, выбор делаем после прохода по всем строкам.
        elif label_raw == "Бюджет":
            week_val = row[col_idx] if len(row) > col_idx else None
            month_val_raw = row[1] if len(row) > 1 else None
            month_val = float(month_val_raw) if _is_numeric(month_val_raw) else None
            if _is_numeric(week_val):
                budget_candidates.append((month_val, float(week_val)))
                logger.debug(
                    "plan_reader: кандидат «Бюджет» — месяц(B)=%s, неделя(кол.%d)=%.2f",
                    month_val, col_idx, float(week_val),
                )

        # ДРР target — строка «% от факта»
        elif label_raw == "% от факта" and unit_target is None:
            val = row[col_idx] if len(row) > col_idx else None
            if _is_numeric(val):
                unit_target = float(val)
                logger.debug("plan_reader: юнитка = %.4f (строка «% от факта», кол.%d)", unit_target, col_idx)

    # Выбор итогового бюджета:
    # 1) Если на вкладке есть валидная строка «Бюджет тотал» — берём её
    #    (максимум по месячному плану B, битые строки с B=0/#REF отсеиваются).
    # 2) Иначе — обычная строка «Бюджет»: максимум по месячному плану B.
    # 3) Иначе (колонка B недоступна на листе) — fallback: первая строка
    #    с числовым недельным значением (случай с #REF! в колонке B).
    positive_total = [c for c in budget_total_candidates if c[0] is not None and c[0] > 0]
    positive_candidates = [c for c in budget_candidates if c[0] is not None and c[0] > 0]

    if positive_total:
        month_val, week_val = max(positive_total, key=lambda c: c[0])
        budget_usd = week_val
        logger.debug(
            "plan_reader: бюджет = %.2f $ (выбрана строка «Бюджет тотал» с макс. месячным планом %.2f, кол.%d)",
            budget_usd, month_val, col_idx,
        )
    elif positive_candidates:
        month_val, week_val = max(positive_candidates, key=lambda c: c[0])
        budget_usd = week_val
        logger.debug(
            "plan_reader: бюджет = %.2f $ (выбрана строка «Бюджет» с макс. месячным планом %.2f, кол.%d)",
            budget_usd, month_val, col_idx,
        )
    elif budget_candidates:
        budget_usd = budget_candidates[0][1]
        logger.debug(
            "plan_reader: бюджет = %.2f $ (fallback: первая числовая строка «Бюджет», кол.%d, B недоступен)",
            budget_usd, col_idx,
        )

    if budget_usd is None:
        return None

    if revenue_plan_lcy is None:
        revenue_plan_lcy = 0.0

    if unit_target is None:
        unit_target = 0.08

    # Если unit_target > 1, значит записан в процентах (15 вместо 0.15)
    if unit_target > 1:
        unit_target = unit_target / 100.0

    return {
        "budget_usd": budget_usd,
        "revenue_plan_lcy": revenue_plan_lcy,
        "unit_target": unit_target,
    }


def _fetch_plan_from_sheets(sheet_id: str, now: datetime) -> dict | None:
    """Скачивает xlsx-экспорт листа Google Sheets и агрегирует план по городам.

    Лист по-городной: суммирует бюджеты/выручку всех городских
    вкладок (реестр _CITY_SHEET_NAMES), unit_target — средневзвешенная по
    revenue_plan города. Если ни одна городская вкладка не найдена — fallback
    на упрощённый путь (единая вкладка с A1=="GENERAL").

    Args:
        sheet_id: Google Spreadsheet ID.
        now: текущая дата/время (для определения текущей недели).

    Returns:
        {week_label, budget_usd, revenue_plan_lcy, unit_target, cities_detail}
        или None при ошибке.
    """
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"

    try:
        resp = requests.get(url, timeout=(10, 30))
    except Exception as exc:
        logger.error("plan_reader: ошибка скачивания xlsx — %s", exc)
        return None

    if resp.status_code != 200:
        logger.error(
            "plan_reader: Google Sheets вернул %d при экспорте %s",
            resp.status_code, sheet_id,
        )
        return None

    try:
        import openpyxl
        from io import BytesIO
        wb = openpyxl.load_workbook(BytesIO(resp.content), data_only=True)
    except Exception as exc:
        logger.error("plan_reader: не удалось открыть xlsx — %s", exc)
        return None

    # Определяем текущую неделю и колонку плана (общая для всех вкладок)
    day_of_month = now.day
    week_num, col_idx, week_day_from = _get_week_col(day_of_month)
    week_day_to = [7, 14, 21, 28, 31][week_num - 1]
    week_label = f"неделя {week_num} ({now.year}-{now.month:02d}-{week_day_from:02d}–{week_day_to:02d})"

    # --- Основной путь: городские вкладки по реестру имён ---
    city_sheet_names_lower = {c.lower() for c in _CITY_SHEET_NAMES}
    matched_cities: dict[str, object] = {}  # canonical_name -> worksheet
    for name in wb.sheetnames:
        stripped = name.strip()
        if stripped.lower() in city_sheet_names_lower:
            matched_cities[stripped] = wb[name]

    if matched_cities:
        total_budget = 0.0
        total_revenue = 0.0
        weighted_unit_sum = 0.0  # unit_target * revenue_plan, для взвешивания
        cities_detail: dict[str, float] = {}
        valid_count = 0

        for city_name, ws in matched_cities.items():
            parsed = _parse_sheet_plan(ws, col_idx)
            if parsed is None:
                logger.warning(
                    "plan_reader: вкладка '%s' — строка «Бюджет» не найдена/битая, исключена из суммы",
                    city_name,
                )
                continue

            valid_count += 1
            total_budget += parsed["budget_usd"]
            total_revenue += parsed["revenue_plan_lcy"]
            weighted_unit_sum += parsed["unit_target"] * parsed["revenue_plan_lcy"]
            cities_detail[city_name] = parsed["budget_usd"]
            logger.info(
                "plan_reader: вкладка '%s' — бюджет $%.2f, выручка ¤%.0f, юнитка %.2f%%",
                city_name, parsed["budget_usd"], parsed["revenue_plan_lcy"], parsed["unit_target"] * 100,
            )

        if valid_count > 0:
            # Средневзвешенная unit_target по revenue_plan города; если суммарная
            # выручка нулевая (все города без «Факт») — простое среднее по бюджету.
            if total_revenue > 0:
                unit_target = weighted_unit_sum / total_revenue
            else:
                unit_target = 0.08

            result = {
                "week_label": week_label,
                "budget_usd": total_budget,
                "revenue_plan_lcy": total_revenue,
                "unit_target": unit_target,
                "cities_detail": cities_detail,
            }
            logger.info(
                "plan_reader: план за %s (по городам: %s) — бюджет $%.0f, выручка ¤%.0f, юнитка %.1f%%",
                week_label, ", ".join(cities_detail.keys()), total_budget, total_revenue, unit_target * 100,
            )
            return result

        logger.warning(
            "plan_reader: ни одна из найденных городских вкладок (%s) не дала валидных данных — "
            "пробуем legacy-путь (A1=='GENERAL')",
            ", ".join(matched_cities.keys()),
        )

    # --- Fallback: упрощённый путь (единая вкладка A1=="GENERAL") ---
    return _fetch_plan_legacy(wb, sheet_id, week_label, col_idx)


def _fetch_plan_legacy(wb, sheet_id: str, week_label: str, col_idx: int) -> dict | None:
    """Упрощённый путь чтения плана: единая вкладка с A1=='GENERAL'.

    Используется как fallback, если по имени не нашлось ни одной городской
    вкладки (лист в формате одной общей вкладки).
    """
    sheet = None
    chosen_sheet_name = None
    for name in wb.sheetnames:
        ws = wb[name]
        try:
            a1_val = ws.cell(row=1, column=1).value
        except Exception:
            a1_val = None
        a1_str = str(a1_val).strip().upper() if a1_val is not None else ""
        if a1_str == "GENERAL":
            sheet = ws
            chosen_sheet_name = name
            logger.info(
                "plan_reader: [legacy] выбрана вкладка '%s' (A1='GENERAL') из листа %s",
                name, sheet_id,
            )
            break

    if sheet is None:
        first_name = wb.sheetnames[0] if wb.sheetnames else None
        if first_name:
            sheet = wb[first_name]
            chosen_sheet_name = first_name
            logger.warning(
                "plan_reader: [legacy] ни у одной вкладки A1!='GENERAL' — fallback на первую '%s'. "
                "Доступные: %s",
                first_name, wb.sheetnames,
            )
        else:
            logger.error("plan_reader: [legacy] книга пуста (нет вкладок) в листе %s", sheet_id)
            return None

    parsed = _parse_sheet_plan(sheet, col_idx)
    if parsed is None:
        logger.error(
            "plan_reader: [legacy] строка «Бюджет» с числовым значением не найдена в '%s' (лист %s)",
            chosen_sheet_name, sheet_id,
        )
        return None

    result = {
        "week_label": week_label,
        "budget_usd": parsed["budget_usd"],
        "revenue_plan_lcy": parsed["revenue_plan_lcy"],
        "unit_target": parsed["unit_target"],
        "cities_detail": {chosen_sheet_name: parsed["budget_usd"]},
    }
    logger.info(
        "plan_reader: [legacy] план за %s — бюджет $%.0f, выручка ¤%.0f, юнитка %.1f%%",
        week_label, result["budget_usd"], result["revenue_plan_lcy"], result["unit_target"] * 100,
    )
    return result


def read_general_plan(sheet_id: str, now: datetime | None = None) -> dict | None:
    """Читает недельный план расходов из Google Sheets (сумма по городским вкладкам).

    Кеш на диск по ключу sheet_id+месяц+неделя, TTL 24ч — не дёргает Google каждый прогон.

    Args:
        sheet_id: Google Spreadsheet ID (из settings autopilot.plan_sheet_id).
        now: текущая дата/время (None = текущий момент по локальному времени).

    Returns:
        {week_label, budget_usd, revenue_plan_lcy, unit_target, cities_detail}
        или None при ошибке. cities_detail — {имя_вкладки: недельный_бюджет_usd},
        для телеметрии/отчётов (потребители контракта budget_usd/revenue_plan_lcy/
        unit_target/week_label не меняются).
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_TZ_LOCAL)

    if not sheet_id:
        logger.error("plan_reader: sheet_id не задан")
        return None

    # Проверяем кеш
    cache = _load_cache()
    key = _cache_key(sheet_id, now)
    cached_entry = cache.get(key)

    if cached_entry:
        age_sec = time.time() - cached_entry.get("saved_at", 0)
        if age_sec < _CACHE_TTL_SEC:
            logger.info(
                "plan_reader: из кеша (возраст %.0f мин) — %s",
                age_sec / 60, cached_entry.get("data", {}).get("week_label"),
            )
            return cached_entry.get("data")
        else:
            logger.info("plan_reader: кеш устарел (%.0f ч) — обновляем", age_sec / 3600)

    # Запрашиваем свежие данные
    result = _fetch_plan_from_sheets(sheet_id, now)
    if result is None:
        # Возвращаем устаревший кеш при ошибке (лучше что-то, чем ничего)
        if cached_entry:
            logger.warning("plan_reader: ошибка обновления — используем устаревший кеш")
            return cached_entry.get("data")
        return None

    # Сохраняем в кеш
    cache[key] = {"saved_at": time.time(), "data": result}
    _save_cache(cache)

    return result
