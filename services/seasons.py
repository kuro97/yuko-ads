"""Сезонный календарь — ключевые периоды спроса (пики и спады продаж) для сервиса с сезонными продуктовыми линейками."""
from datetime import date

SEASONS: list[dict] = [
    {
        "id": "main_peak",
        "name": "Основной пик продаж",
        "start_month": 1,
        "end_month": 5,
        "description": "Основной сезон продаж Продукта B. Максимальный спрос.",
        "budget_modifier": 1.5,
    },
    {
        "id": "proda_promo",
        "name": "Акционный сезон Продукта A",
        "start_month": 3,
        "end_month": 6,
        "description": "Весенняя акция на Продукт A и онбординг новых клиентов.",
        "budget_modifier": 1.3,
    },
    {
        "id": "summer_dip",
        "name": "Летний спад",
        "start_month": 6,
        "end_month": 8,
        "description": "Летние предложения, спрос ниже обычного.",
        "budget_modifier": 0.7,
    },
    {
        "id": "autumn_rebound",
        "name": "Осенний рост спроса",
        "start_month": 9,
        "end_month": 10,
        "description": "Спрос возвращается после летнего спада — второй пик продаж.",
        "budget_modifier": 1.3,
    },
    {
        "id": "winter_holidays",
        "name": "Зимние праздники",
        "start_month": 12,
        "end_month": 1,
        "description": "Праздничный спад, спецпредложения к новому сезону.",
        "budget_modifier": 0.8,
    },
]


def get_all_seasons() -> list[dict]:
    """Возвращает все сезоны (копия SEASONS)."""
    return list(SEASONS)


def get_season_for_date(d: date) -> dict | None:
    """Сезон для указанной даты. Возвращает первый подходящий или None.

    Обычный диапазон (start_month <= end_month): месяц в [start, end].
    Пересечение года (start_month > end_month): месяц >= start ИЛИ месяц <= end.
    """
    month = d.month
    for season in SEASONS:
        start = season["start_month"]
        end = season["end_month"]
        if start <= end:
            if start <= month <= end:
                return season
        else:
            # Пересечение границы года (декабрь-январь)
            if month >= start or month <= end:
                return season
    return None


def get_current_season() -> dict | None:
    """Текущий сезон. Делегирует в get_season_for_date(date.today())."""
    return get_season_for_date(date.today())
