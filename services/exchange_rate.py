"""Курс USD→LCY: настраиваемый источник (с дневным кешем и fallback).

Источник курса задаётся конфигурацией, без привязки к конкретному банку/стране:

  * EXCHANGE_RATE_URL не задан (по умолчанию) — ФИКСИРОВАННЫЙ курс из
    config.USD_TO_LCY (env USD_TO_LCY). Сеть не используется вовсе.
  * EXCHANGE_RATE_URL задан — HTTP GET на этот адрес. Плейсхолдер «{date}» в
    URL заменяется датой в ISO-формате (YYYY-MM-DD); если плейсхолдера нет,
    дата уходит query-параметром ?date=YYYY-MM-DD. Ответ — JSON: либо число,
    либо объект {"rate": <число>} — сколько единиц LCY стоит 1 USD.
    При недоступности источника работает fallback (см. ниже).

Два входа с РАЗНОЙ дисциплиной, и это намеренно:

  * get_usd_to_lcy() — «сколько стоит доллар сейчас». Оперативный расчёт, где
    лучше слегка устаревший курс, чем отсутствие числа: при недоступности
    источника отдаёт последний известный курс, а если и его нет — константу.
  * get_usd_to_lcy_on(day) — курс НА КОНКРЕТНУЮ ДАТУ, строго. Нужен недельным
    когортам (services/cohort_builder.py): ROMI недели обязан быть
    воспроизводим, а курс, подставленный «за неимением лучшего», превращает
    исторический ROMI в число, которое завтра посчитается иначе. Поэтому здесь
    подмены нет вообще: не удалось получить курс за эту дату — None, и ROMI не
    считается (пишется NULL), а не считается по сегодняшнему курсу.
    В режиме фиксированного курса настроенное число и есть источник на любую
    дату — оно воспроизводимо, поэтому подменой не считается.

У функций РАЗДЕЛЬНЫЕ кеши: датированный вход не должен подкладывать чужие даты
в «последний известный курс» оперативного входа.
"""
import logging
import os
from datetime import date, datetime

import requests

from config import USD_TO_LCY as _FALLBACK_RATE

logger = logging.getLogger(__name__)

# Имя env-переменной с адресом внешнего источника курса (необязательная).
_RATE_URL_ENV = "EXCHANGE_RATE_URL"

# Кеш: {'YYYY-MM-DD': rate}. Курс за день не меняется — тянем максимум 1 раз в сутки.
_rate_cache: dict = {}

# Отдельный кеш датированного входа: {'YYYY-MM-DD': rate}. Промахи (None) не
# кешируются — временная недоступность источника не должна навсегда лишать
# неделю курса.
_dated_rate_cache: dict = {}


def _rate_url() -> str:
    """Адрес внешнего источника курса; пустая строка — режим фиксированного курса.

    Читается при каждом вызове, а не при импорте: так настройку можно поменять
    без перезапуска модуля (и подменить в тестах).
    """
    return (os.getenv(_RATE_URL_ENV) or "").strip()


def _fixed_rate() -> float | None:
    """Фиксированный курс из config.USD_TO_LCY; None, если он не положительный."""
    try:
        rate = float(_FALLBACK_RATE)
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


def _parse_rate_payload(payload) -> float | None:
    """Достаёт курс из JSON-ответа источника: число или {"rate": число}."""
    raw = payload.get("rate") if isinstance(payload, dict) else payload
    if isinstance(raw, bool):
        return None
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


def _fetch_usd_lcy(date_str: str) -> float | None:
    """Курс USD→LCY на дату 'YYYY-MM-DD' из настроенного источника. None при ошибке.

    Без EXCHANGE_RATE_URL — фиксированный курс из config (сеть не трогаем).
    """
    url = _rate_url()
    if not url:
        return _fixed_rate()
    try:
        if "{date}" in url:
            resp = requests.get(url.replace("{date}", date_str), timeout=(5, 10))
        else:
            resp = requests.get(url, params={"date": date_str}, timeout=(5, 10))
        if resp.status_code != 200:
            logger.warning("Курс USD→LCY: источник ответил HTTP %s", resp.status_code)
            return None
        rate = _parse_rate_payload(resp.json())
        if rate is None:
            logger.warning("Курс USD→LCY: в ответе источника нет положительного курса")
        return rate
    except Exception as e:
        logger.warning("Курс USD→LCY: ошибка запроса к источнику — %s", e)
        return None


def get_usd_to_lcy() -> float:
    """Текущий курс USD→LCY. Дневной кеш + fallback на config.USD_TO_LCY.

    Используется в расчёте ROMI: расход в долларах переводим в ед.
    """
    today = datetime.now().date().isoformat()

    cached = _rate_cache.get(today)
    if cached:
        return cached

    rate = _fetch_usd_lcy(today)
    if rate is None:
        # Не удалось — пробуем последний известный курс, иначе константу
        if _rate_cache:
            last = list(_rate_cache.values())[-1]
            logger.info("Источник курса недоступен — берём последний курс %.2f", last)
            return last
        logger.info("Источник курса недоступен — fallback на константу %s", _FALLBACK_RATE)
        return float(_FALLBACK_RATE)

    _rate_cache[today] = rate
    logger.info("Курс USD→LCY на %s: %.2f", today, rate)
    return rate


def get_usd_to_lcy_on(day: date) -> float | None:
    """Курс USD→LCY на дату `day` из настроенного источника. None, если курса нет.

    В отличие от get_usd_to_lcy(): никакого fallback — ни на последний
    известный курс, ни на config.USD_TO_LCY как «запасное» число. Историческая
    метрика (ROMI недельной когорты) должна быть воспроизводима, а подставленный
    курс делает её зависимой от того, в какой день её посчитали. Нет курса — нет
    ROMI. (Фиксированный курс — это не подмена, а сам источник: см. докстринг модуля.)

    Args:
        day: дата, на которую нужен курс (в источник уходит как YYYY-MM-DD).

    Returns:
        Курс > 0 либо None (источник недоступен / курса за эту дату нет).
    """
    if not isinstance(day, date) or isinstance(day, datetime):
        raise TypeError("get_usd_to_lcy_on: day должен быть datetime.date")

    date_str = day.isoformat()
    cached = _dated_rate_cache.get(date_str)
    if cached:
        return cached

    rate = _fetch_usd_lcy(date_str)
    if rate is None:
        logger.warning(
            "Источник не отдал курс на %s — ROMI за эту дату не считаем", date_str
        )
        return None

    _dated_rate_cache[date_str] = rate
    logger.info("Курс USD→LCY на %s: %.2f (датированный вход)", date_str, rate)
    return rate
