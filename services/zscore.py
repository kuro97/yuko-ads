"""
Z-score алерты: статистические аномалии по метрикам.

Вычисляет Z-score для CPL каждого города-типа (напр. "CityA L2")
относительно среднего по всем городам с данными.
Z > 2.0 — warning (необычно высокий CPL).
Z > 3.0 — critical (аномально высокий CPL).
"""

import math


def calc_zscore_alerts(cities: list[dict], threshold_warning: float = 2.0,
                       threshold_critical: float = 3.0) -> list[dict]:
    """Генерирует Z-score алерты по CPL городов.

    Args:
        cities: список городов из overview (name, cpl, leads, spend, ...).
        threshold_warning: Z-score для warning (по умолчанию 2.0).
        threshold_critical: Z-score для critical (по умолчанию 3.0).

    Returns:
        Список алертов [{level, city, message, zscore, metric, value, mean, std}].
    """
    # Берём только города с реальными данными (лиды > 0, CPL > 0)
    valid = [c for c in cities if c.get("leads", 0) > 0 and c.get("cpl", 0) > 0]

    if len(valid) < 3:
        # Мало данных для статистики — нет алертов
        return []

    cpls = [c["cpl"] for c in valid]
    mean_cpl = sum(cpls) / len(cpls)
    std_cpl = _std(cpls, mean_cpl)

    if std_cpl == 0:
        # Все CPL одинаковые — нет аномалий
        return []

    alerts = []
    for city in valid:
        z = (city["cpl"] - mean_cpl) / std_cpl

        if z >= threshold_critical:
            alerts.append(_make_alert("critical", city, z, mean_cpl, std_cpl))
        elif z >= threshold_warning:
            alerts.append(_make_alert("warning", city, z, mean_cpl, std_cpl))

    # Сортировка: critical → warning, потом по Z-score убывание
    priority = {"critical": 0, "warning": 1}
    alerts.sort(key=lambda a: (priority.get(a["level"], 99), -a["zscore"]))

    return alerts


def _make_alert(level: str, city: dict, z: float, mean: float, std: float) -> dict:
    """Создать алерт."""
    return {
        "level": level,
        "city": city["name"],
        "message": f"CPL = {city['cpl']}$ — Z-score {z:.1f} (среднее {mean:.1f}$, σ={std:.1f})",
        "zscore": round(z, 2),
        "metric": "cpl",
        "value": city["cpl"],
        "mean": round(mean, 2),
        "std": round(std, 2),
    }


def _std(values: list[float], mean: float) -> float:
    """Стандартное отклонение (population)."""
    if len(values) < 2:
        return 0.0
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return math.sqrt(variance)
