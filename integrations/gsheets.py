"""Интеграция с Google Sheets — чтение исторических данных по лидам."""

import json
import os
import re
import logging
from datetime import datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

CREDENTIALS_PATH = os.getenv(
    "GOOGLE_CREDENTIALS_PATH",
    os.path.join(os.path.dirname(__file__), "..", "data", "google-credentials.json"),
)

CACHE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "historical_data.json"
)

# Маппинг названий городов из имён файлов
CITY_ALIASES = {
    "citya": "CityA",
    "cityb": "CityB",
    "cityc": "CityC",
    "citye": "CityE",
    "cityd": "CityD",
    "онлайн": "Онлайн",
}

# Маппинг месяцев (русские названия -> номер)
MONTH_MAP = {
    "январь": 1, "февраль": 2, "март": 3, "марта": 3,
    "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9,
    "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}


def _get_client() -> gspread.Client:
    """Создаёт авторизованный gspread клиент."""
    creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def _parse_city_from_name(name: str) -> str | None:
    """Извлекает город из названия таблицы."""
    name_lower = name.lower()
    for alias, city in CITY_ALIASES.items():
        if alias in name_lower:
            return city
    return None


def _parse_expense(val: str) -> float:
    """Парсит расходы: '$12,34' -> 12.34"""
    if not val:
        return 0.0
    cleaned = val.replace("$", "").replace("\xa0", "").replace(" ", "").strip()
    # Русский формат: запятая как дробный разделитель
    cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_number(val: str) -> float:
    """Парсит число: '12,5' -> 12.5"""
    if not val:
        return 0.0
    cleaned = val.replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def _parse_date(cell_val: str) -> str | None:
    """Извлекает дату из 'Всего (DD.MM.YYYY)' -> 'YYYY-MM-DD'"""
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", cell_val)
    if match:
        day, month, year = match.groups()
        return f"{year}-{month}-{day}"
    return None


def fetch_all_historical_data(force_refresh: bool = False) -> list[dict]:
    """Загружает все исторические данные из Google Sheets.

    Возвращает список записей:
    [{"city": "CityA", "date": "2025-01-01", "leads": 10, "spend": 50.00, "cpl": 5.00}, ...]

    Результат кешируется в data/historical_data.json.
    """
    # Проверяем кеш (обновляем раз в день)
    if not force_refresh and os.path.exists(CACHE_PATH):
        mtime = datetime.fromtimestamp(os.path.getmtime(CACHE_PATH))
        if datetime.now() - mtime < timedelta(hours=24):
            logger.info("Используем кеш исторических данных")
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)

    logger.info("Загружаем исторические данные из Google Sheets...")
    gc = _get_client()

    # Находим все отчёты по лидам
    files = gc.list_spreadsheet_files()
    reports = [
        f for f in files
        if "Отчет по лидам" in f["name"]
        and "Копия" not in f["name"]
        and "не прав" not in f["name"]
    ]

    all_data = []
    processed = 0
    errors = 0

    for report in reports:
        name = report["name"]
        city = _parse_city_from_name(name)
        if not city:
            logger.warning("Не определён город для: %s", name)
            continue

        try:
            sh = gc.open_by_key(report["id"])
            # Ищем вкладку Сводная по дате
            ws = None
            for worksheet in sh.worksheets():
                if "водна" in worksheet.title.lower() and "дат" in worksheet.title.lower():
                    ws = worksheet
                    break

            if not ws:
                logger.warning("Нет вкладки 'Сводная по дате' в: %s", name)
                continue

            rows = ws.get_all_values()
            if len(rows) < 2:
                continue

            # Парсим строки с данными
            for row in rows[1:]:  # Пропускаем заголовок
                if len(row) < 7:
                    continue

                date_str = _parse_date(row[0])
                if not date_str:
                    continue

                leads = _parse_number(row[4])
                spend = _parse_expense(row[6])

                if leads == 0 and spend == 0:
                    continue

                cpl = round(spend / leads, 2) if leads > 0 else 0

                all_data.append({
                    "city": city,
                    "date": date_str,
                    "leads": int(leads),
                    "spend": round(spend, 2),
                    "cpl": cpl,
                })

            processed += 1
            logger.info("Обработан: %s (%s)", name, city)

        except Exception as e:
            errors += 1
            logger.error("Ошибка при обработке %s: %s", name, e)

    logger.info(
        "Загружено: %d записей из %d отчётов (%d ошибок)",
        len(all_data), processed, errors,
    )

    # Убираем дубли (один город-дата может быть в нескольких файлах)
    seen = set()
    unique_data = []
    for d in sorted(all_data, key=lambda x: (x["city"], x["date"])):
        key = (d["city"], d["date"])
        if key not in seen:
            seen.add(key)
            unique_data.append(d)

    # Сохраняем кеш
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(unique_data, f, ensure_ascii=False, indent=2)

    logger.info("Кеш сохранён: %d уникальных записей", len(unique_data))
    return unique_data


def get_seasonal_insights(city: str | None = None) -> list[dict]:
    """Формирует сезонные инсайты для гипотез.

    Анализирует:
    - Лучшие/худшие недели по CPL для каждого города
    - Тренды: рост или снижение лидов
    - Сезонные паттерны (месяц к месяцу)

    Возвращает список инсайтов для generate_hypotheses().
    """
    data = fetch_all_historical_data()
    if not data:
        return []

    insights = []

    # Группируем по городу и месяцу
    from collections import defaultdict
    city_month = defaultdict(lambda: {"leads": 0, "spend": 0.0, "days": 0})

    for d in data:
        if city and d["city"] != city:
            continue
        month_key = d["date"][:7]  # YYYY-MM
        key = (d["city"], month_key)
        city_month[key]["leads"] += d["leads"]
        city_month[key]["spend"] += d["spend"]
        city_month[key]["days"] += 1

    # Вычисляем CPL по месяцам
    monthly_stats = {}
    for (c, month), stats in city_month.items():
        cpl = round(stats["spend"] / stats["leads"], 2) if stats["leads"] > 0 else 0
        monthly_stats[(c, month)] = {
            "city": c,
            "month": month,
            "leads": stats["leads"],
            "spend": round(stats["spend"], 2),
            "cpl": cpl,
            "days": stats["days"],
        }

    # Для каждого города: лучший и худший месяц
    cities_data = defaultdict(list)
    for (c, month), stats in monthly_stats.items():
        if stats["leads"] >= 10:  # Минимум для статистической значимости
            cities_data[c].append(stats)

    now = datetime.now()
    current_month = now.strftime("%Y-%m")
    # Месяц год назад (для сезонного сравнения)
    last_year_month = f"{now.year - 1}-{now.month:02d}"

    for c, months in cities_data.items():
        months.sort(key=lambda x: x["month"])

        if len(months) < 2:
            continue

        best = min(months, key=lambda x: x["cpl"])
        worst = max(months, key=lambda x: x["cpl"])

        # Инсайт: лучший месяц
        best_month_name = _month_name(best["month"])
        worst_month_name = _month_name(worst["month"])

        insights.append({
            "type": "seasonal",
            "city": c,
            "title": f"{c}: лучший месяц — {best_month_name}",
            "description": (
                f"CPL ${best['cpl']} ({best['leads']} лидов). "
                f"Худший: {worst_month_name} — CPL ${worst['cpl']}."
            ),
            "best_month": best["month"],
            "worst_month": worst["month"],
        })

        # Инсайт: прошлогодние данные для текущего месяца
        last_year = [m for m in months if m["month"] == last_year_month]
        if last_year:
            ly = last_year[0]
            insights.append({
                "type": "seasonal_comparison",
                "city": c,
                "title": f"{c}: в {_month_name(last_year_month)} прошлого года",
                "description": (
                    f"CPL ${ly['cpl']}, {ly['leads']} лидов за {ly['days']} дн, "
                    f"расход ${ly['spend']}."
                ),
                "reference_month": last_year_month,
            })

        # Тренд последних 3 месяцев
        recent = [m for m in months if m["month"] >= (now - timedelta(days=90)).strftime("%Y-%m")]
        if len(recent) >= 2:
            recent.sort(key=lambda x: x["month"])
            first_cpl = recent[0]["cpl"]
            last_cpl = recent[-1]["cpl"]
            if first_cpl > 0:
                change_pct = round((last_cpl - first_cpl) / first_cpl * 100)
                direction = "растёт" if change_pct > 10 else "снижается" if change_pct < -10 else "стабильный"
                insights.append({
                    "type": "trend",
                    "city": c,
                    "title": f"{c}: CPL {direction} ({change_pct:+d}%)",
                    "description": (
                        f"{_month_name(recent[0]['month'])}: CPL ${first_cpl} → "
                        f"{_month_name(recent[-1]['month'])}: CPL ${last_cpl}."
                    ),
                    "change_pct": change_pct,
                })

    return insights


def _month_name(month_str: str) -> str:
    """Конвертирует 'YYYY-MM' в русское название: 'январь 2025'."""
    month_names = {
        1: "январь", 2: "февраль", 3: "март", 4: "апрель",
        5: "май", 6: "июнь", 7: "июль", 8: "август",
        9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
    }
    try:
        parts = month_str.split("-")
        year = parts[0]
        month_num = int(parts[1])
        return f"{month_names.get(month_num, '?')} {year}"
    except (IndexError, ValueError):
        return month_str
