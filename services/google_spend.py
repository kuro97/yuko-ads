"""
Снимок дневного расхода Google Ads из Google Sheets.

Таблица (sheet_id=1ExampleSheetId000000000000000000000000000000) — контракторская
выгрузка Google Ads. ВАЖНО: выгрузка создаёт НОВУЮ ВКЛАДКУ НА КАЖДЫЙ ДЕНЬ
с именем «DD.MM» (например «10.06», «06.07»), gid каждой новой вкладки заранее
неизвестен — поэтому фиксированный gid для CSV-экспорта больше не работает
(бот годами читал одну и ту же старую вкладку и не видел новых дней).

Актуальная навигация:
  1. Скачиваем ВЕСЬ файл целиком через xlsx-экспорт (openpyxl).
  2. Ищем нужную вкладку СНАЧАЛА по имени «DD.MM» дня.
  3. Если такой вкладки нет (встречаются вкладки с «кривыми» именами вроде
     «07,04» или «0804») — сканируем A1 всех вкладок: там период вида
     «10 июня 2026 г. - 10 июня 2026 г.» — берём первую с совпадающей датой.

Структура вкладки (и раньше в CSV, и сейчас в xlsx):
  Строка 1 (A1)    : период "28 июня 2026 г. - 28 июня 2026 г."
  Строка 2         : заголовки (Статус кампании, Кампания, ..., Расходы, ...)
  Строки 3+        : данные кампаний
  Строка «Итого»   : первая ячейка = "Итого (Кампании)", колонка «Расходы» = сумма ($)

Колонку «Расходы» ищем ПО ЗАГОЛОВКУ строки-шапки, а не по фиксированному
индексу: если подрядчик вставит лишнюю колонку (например «Бюджет»), «Расходы»
съедут, и по фиксированному индексу вместо расхода прочитается соседнее
значение (CTR).
Фолбэк на исторический индекс 7 с warning, если заголовок не найден.

Расход читать ТОЛЬКО из строки «Итого (Кампании)» — не суммировать строки
(есть подытоги → двойной счёт).

История сохраняется в data/google_daily_spend.json как {date_iso: spend_usd}.
Недельная сумма набирается за ~7 дней ежедневных снимков.
"""

import json
import logging
import re
from datetime import date, datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path

import openpyxl
import requests

logger = logging.getLogger(__name__)

# Путь к файлу истории дневных расходов
_SPEND_FILE = Path(__file__).resolve().parent.parent / "data" / "google_daily_spend.json"

# Локальное время (UTC+5 по умолчанию, настраивается этой константой) — единое со всеми остальными модулями проекта
_TZ_LOCAL = timezone(timedelta(hours=5))

# Словарь русских месяцев → номер месяца
_RU_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

# Исторический fallback-индекс колонки «Расходы» (0-based). Используется, только
# если колонку не удалось определить по заголовку строки-шапки.
_SPEND_COLUMN_INDEX = 7

# Префикс заголовка колонки расхода в строке-шапке. Сверяем по нормализованному
# префиксу — устойчиво к регистру, лишним пробелам и суффиксам вроде «, $».
_SPEND_HEADER_PREFIX = "расход"


def _normalize_header(cell) -> str:
    """Нормализует заголовок ячейки: strip + схлопывание пробелов + lower."""
    return re.sub(r"\s+", " ", str(cell).strip()).lower()


def _spend_index_from_header(row) -> int | None:
    """0-based индекс колонки «Расходы» в строке-шапке (или None, если её нет).

    Сверяем по префиксу нормализованного заголовка: «Расходы», «РАСХОДЫ»,
    « Расходы », «Расходы, $» — всё распознаётся, а позиция колонки может быть
    любой (подрядчик может вставить лишние колонки, напр. «Бюджет»).
    """
    for idx, cell in enumerate(row):
        if cell is None:
            continue
        if _normalize_header(cell).startswith(_SPEND_HEADER_PREFIX):
            return idx
    return None


def _yesterday_local() -> date:
    """Возвращает вчерашнюю дату по локальному времени (используется по умолчанию)."""
    return (datetime.now(_TZ_LOCAL) - timedelta(days=1)).date()


def _parse_ru_date(text: str) -> str | None:
    """Парсит русскую дату вида «28 июня 2026 г.» → «2026-06-28».

    Берёт ПЕРВУЮ дату из строки (период «от - до» — берём левую часть).

    Returns:
        ISO-строка YYYY-MM-DD или None при ошибке разбора.
    """
    # Убираем лишнее: "г.", точки, лишние пробелы
    cleaned = text.strip()
    # Ищем паттерн: число + пробел + месяц(рус) + пробел + год
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", cleaned, re.IGNORECASE)
    if not m:
        logger.warning("google_spend: не удалось разобрать дату из «%s»", text[:80])
        return None
    day = int(m.group(1))
    month_ru = m.group(2).lower()
    year = int(m.group(3))
    month = _RU_MONTHS.get(month_ru)
    if month is None:
        logger.warning("google_spend: неизвестный месяц «%s» в строке «%s»", month_ru, text[:80])
        return None
    return f"{year}-{month:02d}-{day:02d}"


def _parse_spend_value(raw: str) -> float:
    """Парсит строку расхода «189,42» или «1 234,56» → 189.42 / 1234.56.

    Google Sheets экспортирует числа с запятой-десятичной и пробелом-тысячным.
    """
    # Убираем пробелы (разделитель тысяч), заменяем запятую на точку
    cleaned = raw.strip().replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        logger.warning("google_spend: не удалось разобрать расход «%s»", raw)
        return 0.0


def _download_workbook(sheet_id: str):
    """Скачивает ВЕСЬ файл таблицы в формате xlsx и парсит его openpyxl.

    CSV-экспорт по фиксированному gid больше не годится: выгрузка создаёт
    новую вкладку на каждый день, а gid новых вкладок заранее неизвестен.
    Поэтому качаем файл целиком и потом ищем нужную вкладку по имени/A1.

    Returns:
        openpyxl.Workbook или None при ошибке скачивания/разбора.
    """
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"
    try:
        resp = requests.get(url, timeout=(10, 60))
    except Exception as exc:
        logger.error("google_spend: ошибка скачивания xlsx — %s", exc)
        return None

    if resp.status_code != 200:
        logger.error(
            "google_spend: Google Sheets вернул %d для %s (xlsx export)",
            resp.status_code, sheet_id,
        )
        return None

    try:
        return openpyxl.load_workbook(BytesIO(resp.content), data_only=True)
    except Exception as exc:
        logger.error("google_spend: ошибка разбора xlsx — %s", exc)
        return None


def _find_target_sheet(wb, target_date: date) -> tuple[str, str] | None:
    """Ищет вкладку файла, соответствующую нужной дате.

    Сначала — по имени «DD.MM» (формат ежедневных вкладок выгрузки).
    Если такой вкладки нет — сканирует A1 всех вкладок (период
    «D месяца ГГГГ г. - ...») и берёт первую с совпадающей датой.

    Returns:
        (имя_вкладки, date_iso) или None если вкладка не найдена.
    """
    tab_name = f"{target_date.day:02d}.{target_date.month:02d}"
    target_iso = target_date.isoformat()

    if tab_name in wb.sheetnames:
        return tab_name, target_iso

    # Fallback: сканируем A1 всех вкладок — встречаются «кривые» имена
    # вроде «07,04» или «0804», где прямой поиск по имени не сработает.
    for name in wb.sheetnames:
        a1 = wb[name]["A1"].value
        if not a1:
            continue
        date_iso = _parse_ru_date(str(a1))
        if date_iso == target_iso:
            return name, date_iso

    return None


def _extract_spend_from_sheet(ws) -> float | None:
    """Читает суммарный расход из строки «Итого (Кампании)» вкладки.

    Колонку «Расходы» определяем ПО ЗАГОЛОВКУ строки-шапки (не по фиксированному
    индексу): подрядчик может вставить лишнюю колонку (например «Бюджет»)
    и позиция «Расходы» съедет — тогда по индексу 7 читается чужое
    значение (CTR). Fallback — исторический индекс 7 с warning, если заголовок
    не найден.

    Расход берём ТОЛЬКО из строки «Итого (Кампании)» — не суммируем строки
    кампаний (есть подытоги → двойной счёт).

    Returns:
        Расход в USD или None если строка «Итого» не найдена/некорректна.
    """
    spend_idx: int | None = None

    for row in ws.iter_rows(values_only=True):
        if not row:
            continue

        # Пока колонка не найдена — ищем её по заголовку строки-шапки. Шапка
        # идёт выше строки «Итого», поэтому к моменту «Итого» индекс уже готов.
        if spend_idx is None:
            spend_idx = _spend_index_from_header(row)

        if row[0] is None:
            continue
        first_cell = str(row[0]).strip()
        if not first_cell.startswith("Итого"):
            continue

        # Заголовок «Расходы» не встретился — фолбэк на исторический индекс 7.
        idx = spend_idx
        if idx is None:
            logger.warning(
                "google_spend: колонка «Расходы» не найдена по заголовку во вкладке "
                "«%s» — фолбэк на индекс %d", ws.title, _SPEND_COLUMN_INDEX,
            )
            idx = _SPEND_COLUMN_INDEX

        if len(row) <= idx:
            logger.warning(
                "google_spend: строка «%s» короче %d колонок (%d)",
                first_cell, idx + 1, len(row),
            )
            continue

        raw = row[idx]
        # xlsx-экспорт отдаёт числа как float/int напрямую (в отличие от
        # старого CSV, где было "189,42" строкой) — обрабатываем оба случая.
        if isinstance(raw, (int, float)):
            return float(raw)
        return _parse_spend_value(str(raw))

    return None


def read_daily_google_spend(
    sheet_id: str,
    target_date: date | None = None,
) -> tuple[str, float] | None:
    """Скачивает xlsx целиком, находит вкладку нужного дня, парсит расход.

    Args:
        sheet_id: Google Spreadsheet ID.
        target_date: дата снимка (по умолчанию — вчера по локальному времени).

    Returns:
        (date_iso, spend_usd) или None при ошибке / отсутствии вкладки.
    """
    if target_date is None:
        target_date = _yesterday_local()

    wb = _download_workbook(sheet_id)
    if wb is None:
        return None

    found = _find_target_sheet(wb, target_date)
    if found is None:
        logger.warning(
            "google_spend: вкладка за %s не найдена (искали имя «%02d.%02d» и период по A1)",
            target_date.isoformat(), target_date.day, target_date.month,
        )
        return None

    tab_name, date_iso = found
    spend_usd = _extract_spend_from_sheet(wb[tab_name])
    if spend_usd is None:
        logger.error(
            "google_spend: строка «Итого (Кампании)» не найдена во вкладке «%s»", tab_name,
        )
        return None

    logger.info(
        "google_spend: %s (вкладка «%s») → расход $%.2f", date_iso, tab_name, spend_usd,
    )
    return date_iso, spend_usd


def _load_spend_history() -> dict:
    """Загружает историю дневных расходов. При ошибке — пустой dict."""
    if not _SPEND_FILE.exists():
        return {}
    try:
        return json.loads(_SPEND_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("google_spend: ошибка чтения истории — %s", exc)
        return {}


def _save_spend_history(data: dict) -> None:
    """Атомарно сохраняет историю (tmp → rename)."""
    _SPEND_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SPEND_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_SPEND_FILE)
    except Exception as exc:
        logger.error("google_spend: не удалось сохранить историю — %s", exc)
        raise


def capture_daily_google_spend(sheet_id: str, target_date: date | None = None) -> None:
    """Читает снимок за вчера (или указанный день) и дописывает в историю (upsert по дате).

    Старые дни не удаляются — история накапливается.
    Вызывается ежедневным кроном.

    Args:
        sheet_id: Google Spreadsheet ID.
        target_date: дата снимка (по умолчанию — вчера по локальному времени).
    """
    result = read_daily_google_spend(sheet_id, target_date=target_date)
    if result is None:
        logger.error("google_spend: capture_daily_google_spend: не удалось прочитать снимок")
        return

    date_iso, spend_usd = result
    history = _load_spend_history()
    history[date_iso] = spend_usd
    _save_spend_history(history)
    logger.info("google_spend: сохранён расход %s → $%.2f", date_iso, spend_usd)


def backfill_google_spend_from_tabs(
    sheet_id: str,
    since_iso: str | None = None,
    update_existing: bool = False,
) -> dict:
    """Проход по вкладкам-датам файла — восстанавливает/дотягивает историю.

    Два режима:
    - Разовый backfill (since_iso=None, update_existing=False — как было
      исторически): проходит ВСЕ вкладки, добавляет только отсутствующие дни,
      существующие снимки НЕ трогает. Нужен был один раз после смены навигации
      (раньше бот годами читал одну и ту же старую вкладку по фиксированному gid
      и не видел новых дней — история почти пустая).
    - Ежедневный рескан окна (since_iso задан — см. rescan_recent_google_spend):
      рассматривает ТОЛЬКО вкладки с датой >= since_iso (последние N дней).
      При update_existing=True внутри окна ещё и ПЕРЕЗАПИСЫВАЕТ уже сохранённый
      день, если подрядчик залил/поправил вкладку и число изменилось —
      «недостающие/изменившиеся дни» дотягиваются. Идемпотентно: если ничего
      не изменилось — на диск не пишем.

    Args:
        sheet_id: Google Spreadsheet ID.
        since_iso: нижняя граница окна "YYYY-MM-DD" включительно; вкладки старше
            игнорируются (None — без ограничения, разовый backfill по всему файлу).
        update_existing: True — внутри окна перезаписывать день при изменении
            значения (False — существующие дни не трогаем, старое поведение).

    Returns:
        dict со статистикой: {"added": int, "updated": int,
        "skipped_existing": int, "unparsed": int,
        "days": [список добавленных+обновлённых date_iso]}.
    """
    empty_stats = {"added": 0, "updated": 0, "skipped_existing": 0, "unparsed": 0, "days": []}

    wb = _download_workbook(sheet_id)
    if wb is None:
        logger.error("google_spend: backfill — не удалось скачать файл")
        return empty_stats

    history = _load_spend_history()
    added_days: list[str] = []
    updated_days: list[str] = []
    skipped_existing = 0
    unparsed = 0

    for name in wb.sheetnames:
        ws = wb[name]
        a1 = ws["A1"].value
        date_iso = _parse_ru_date(str(a1)) if a1 else None
        if date_iso is None:
            unparsed += 1
            continue

        # Ограничение окна: вкладки старше нижней границы не рассматриваем
        # (ежедневный рескан работает только по последним N дням).
        if since_iso is not None and date_iso < since_iso:
            continue

        exists = date_iso in history
        if exists and not update_existing:
            skipped_existing += 1
            continue

        spend_usd = _extract_spend_from_sheet(ws)
        if spend_usd is None:
            logger.warning(
                "google_spend: backfill — вкладка «%s» (%s): строка «Итого» не найдена",
                name, date_iso,
            )
            unparsed += 1
            continue

        if exists:
            # update_existing=True: перезаписываем ТОЛЬКО при реальном изменении
            # значения (идемпотентность — без изменений история не переписывается).
            if float(history[date_iso]) == float(spend_usd):
                skipped_existing += 1
                continue
            history[date_iso] = spend_usd
            updated_days.append(date_iso)
        else:
            history[date_iso] = spend_usd
            added_days.append(date_iso)

    if added_days or updated_days:
        _save_spend_history(history)

    logger.info(
        "google_spend: backfill завершён — добавлено %d, обновлено %d, уже было %d, не распознано %d",
        len(added_days), len(updated_days), skipped_existing, unparsed,
    )
    return {
        "added": len(added_days),
        "updated": len(updated_days),
        "skipped_existing": skipped_existing,
        "unparsed": unparsed,
        "days": sorted(added_days + updated_days),
    }


def rescan_recent_google_spend(
    sheet_id: str,
    days: int = 7,
    now: date | None = None,
) -> dict:
    """Ежедневный рескан последних `days` дней: недостающие/изменившиеся дни
    дотягиваются из появившихся/поправленных вкладок подрядчика.

    Зачем: подрядчик грузит вкладки с задержкой (иногда пачкой за неделю). Крон,
    читающий только «вчера», навсегда оставлял пропущенный день без снимка —
    расход не дотягивался, когда вкладка появлялась (однажды уже восстанавливали
    53 дня руками через backfill_google_spend_from_tabs). Рескан окна закрывает
    эту дыру. Переиспользует backfill_google_spend_from_tabs с ограничением окна
    и update_existing=True. Идемпотентно.

    Args:
        sheet_id: Google Spreadsheet ID.
        days: размер окна назад от `now` включительно (дефолт 7).
        now: опорная дата (по умолчанию — сегодня по локальному времени).

    Returns:
        Статистика backfill_google_spend_from_tabs по окну.
    """
    if now is None:
        now = datetime.now(_TZ_LOCAL).date()
    since_iso = (now - timedelta(days=days - 1)).isoformat()
    stats = backfill_google_spend_from_tabs(sheet_id, since_iso=since_iso, update_existing=True)
    logger.info(
        "google_spend: рескан последних %d дней (с %s) — добавлено %d, обновлено %d",
        days, since_iso, stats.get("added", 0), stats.get("updated", 0),
    )
    return stats


def get_google_week_spend(date_from_iso: str, date_to_iso: str) -> float:
    """Суммарный расход Google Ads за диапазон дат из сохранённой истории.

    Дни без снимка считаются = 0 (логируем сколько дней покрыто).

    Args:
        date_from_iso: начало периода "YYYY-MM-DD" (включительно).
        date_to_iso: конец периода "YYYY-MM-DD" (включительно).

    Returns:
        Суммарный расход в USD (float).
    """
    history = _load_spend_history()

    total = 0.0
    covered = 0
    missing = []

    # Итерируем по дням периода
    d_from = date.fromisoformat(date_from_iso)
    d_to = date.fromisoformat(date_to_iso)
    current = d_from
    while current <= d_to:
        iso = current.isoformat()
        if iso in history:
            total += float(history[iso])
            covered += 1
        else:
            missing.append(iso)
        current += timedelta(days=1)

    total_days = (d_to - d_from).days + 1
    logger.info(
        "get_google_week_spend: %s – %s → $%.2f (%d/%d дней покрыто, нет: %s)",
        date_from_iso, date_to_iso, total,
        covered, total_days,
        missing if missing else "—",
    )
    return total


def get_daily_google_spend(date_iso: str) -> float | None:
    """Расход Google за КОНКРЕТНЫЙ день из сохранённой истории.

    Различаем «0» и «нет данных»:
    - float (в т.ч. настоящий 0.0) — снимок дня ЕСТЬ (реклама реально не
      крутилась в этот день → честный ноль);
    - None — снимка за день НЕТ (вкладку подрядчик ещё не залил). Раньше
      отсутствие дня отдавалось потребителям как «$0» и вводило владельца в
      заблуждение — теперь возвращаем None, чтобы вызывающий показал честное
      «данных ещё нет».
    """
    history = _load_spend_history()
    val = history.get(date_iso)
    return float(val) if val is not None else None


def get_last_known_google_spend(
    on_or_before_iso: str | None = None,
) -> tuple[str, float] | None:
    """Последний ИЗВЕСТНЫЙ день Google-расхода из истории.

    Нужен для честной строки «последний известный день DD.MM: $X», когда за
    искомый день снимка ещё нет (подрядчик грузит с задержкой).

    Args:
        on_or_before_iso: если задан — берём последний день с датой <= этой
            (обычно передаём искомый день, для которого данных нет).

    Returns:
        (date_iso, spend_usd) самого свежего подходящего дня, либо None, если
        подходящей истории нет вовсе.
    """
    history = _load_spend_history()
    if not history:
        return None
    # ISO-строки YYYY-MM-DD сравниваются лексикографически = хронологически
    candidates = [
        (d, v) for d, v in history.items()
        if v is not None and (on_or_before_iso is None or d <= on_or_before_iso)
    ]
    if not candidates:
        return None
    date_iso, spend = max(candidates, key=lambda kv: kv[0])
    return date_iso, float(spend)


def count_missing_google_days(date_from_iso: str, date_to_iso: str) -> int:
    """Сколько дней в диапазоне [from, to] БЕЗ снимка Google-расхода.

    Нужен для пометки «Google неполный за N дней» в подписи ДРР: окно ДРР молча
    суммирует отсутствующие дни как 0 (get_google_week_spend), и без этого
    счётчика неполнота Google-данных не видна.
    """
    history = _load_spend_history()
    d_from = date.fromisoformat(date_from_iso)
    d_to = date.fromisoformat(date_to_iso)
    missing = 0
    current = d_from
    while current <= d_to:
        if current.isoformat() not in history:
            missing += 1
        current += timedelta(days=1)
    return missing
