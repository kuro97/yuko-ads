"""Сверка/маппинг реальных платежей ERP (CDP) к объявлениям creative_kb.

Цепочка: платёж ERP (contract_number) → маппинг lead_id→ad_id (через AMO
fb_ad_id 902422) → агрегат payments_erp/revenue_erp_lcy на объявление за окно.
Пишем в creative_kb. Источник-каскад для решений: ERP (primary) → AMO (fallback),
объединение = max (fail-closed — сигнал оплаты не теряется, лучше ложно-позитив,
чем пропуск).

ВАЖНО: поле payment["deal_id"] — внутренний surrogate-ключ CDP, НЕ AMO lead_id
(маппинг по deal_id не находит платящих лидов). Настоящий AMO lead_id лежит в
payment["contract_number"] (проверяется точечными get_lead — contract_number
резолвится в существующие лиды). Маппинг строится по contract_number,
deal_id в платеже игнорируется (см. docs/specs/ARCH-cdp-payments.md §10.2).

Fail-closed: любая ошибка CDP → AMO-поля в creative_kb НЕ затираем, возвращаем
частичный/пустой результат. Комментарии на русском.

ДВУХЪЯРУСНЫЙ МАППИНГ: пакетное окно _build_deal_to_ad_map (100 дней
get_leads_window) не покрывает случай «оплата пришла сейчас за сделку, созданную
несколько месяцев назад» — лид создан раньше окна, contract_number не находится.
Второй ярус — точечные integrations.amo.get_lead(lead_id) для
contract_number, не покрытых пакетным окном, с дисковым кешем
(data/cdp_payments_lead_cache.json: lead_id→ad_id|null) и потолком запросов за
прогон (см. _POINTED_LOOKUP_CAP). Кеш переживает прогоны — повторные вызовы не
передёргивают AMO ни для найденных, ни для не найденных (null-кеш) лидов.
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from services import cdp_client, state_store
from services.cdp_client import CdpError

logger = logging.getLogger(__name__)

# State-файл: last_synced_doc_date — чтобы не перетягивать всю историю.
_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "cdp_payments_state.json"

# Дисковый кеш точечных lookup'ов lead_id→ad_id|None (второй ярус маппинга).
# Персистентный между прогонами — не передёргивает AMO повторно ни для найденных,
# ни для не сопоставленных (null) лидов.
_LEAD_CACHE_FILE = Path(__file__).resolve().parent.parent / "data" / "cdp_payments_lead_cache.json"

# Сколько дней AMO-лидов смотрим при построении маппинга lead_id→ad_id (1 ярус).
# Аналог months_back в amo_outcomes — с запасом, т.к. сделка могла быть создана
# раньше окна платежей (платёж приходит позже создания лида).
_DEAL_MAP_LOOKBACK_DAYS = 100

# Потолок точечных get_lead() за один прогон (2 ярус) — защита от перегрузки AMO,
# если пакетное окно вдруг не покрыло аномально много contract_number.
_POINTED_LOOKUP_CAP = 300

# Троттлинг между точечными get_lead() (как в integrations.amo.get_latest_fb_lead_ts).
_POINTED_LOOKUP_THROTTLE_SEC = 0.2

# ---------------------------------------------------------------------------
# Окно РОВНО 7 календарных дней в едином локальном TZ (Wave 3A — честные 7d)
# ---------------------------------------------------------------------------
_TZ_LOCAL = timezone(timedelta(hours=5))

# Длина честного окна оплат в календарных днях (ровно 7).
SEVEN_D_DAYS = 7


def seven_day_window_local(now: datetime | None = None) -> dict:
    """Явные границы окна РОВНО 7 календарных суток в локальном TZ.

    Окно — полуоткрытый интервал [window_from, window_to): window_to = сегодняшняя
    локальная дата (00:00, ИСКЛючительно — сегодняшний неполный день не входит),
    window_from = window_to − 7 дней (ВКЛючительно). Итого ровно 7 полных
    прошедших суток. Единый локальный TZ для обоих источников (AMO по created_at,
    ERP по doc_date) — чтобы «7 дней» значило одно и то же везде.

    Возвращает dict:
      window_from   — ISO date, включительная нижняя граница (напр. '2026-07-10')
      window_to     — ISO date, ИСКЛючительная верхняя граница (= сегодня, '2026-07-17')
      cdp_date_from — date, включительно (для cdp_client.get_payments — фильтр по doc_date)
      cdp_date_to   — date, ВКЛючительно последний вошедший день (window_to − 1 день)
      amo_from_ts   — int unix, 00:00 локального времени window_from (включительно)
      amo_to_ts     — int unix, последняя секунда перед window_to 00:00 (исключая window_to)
    """
    now_local = (now or datetime.now(tz=_TZ_LOCAL)).astimezone(_TZ_LOCAL)
    today = now_local.date()
    window_from_date = today - timedelta(days=SEVEN_D_DAYS)   # включительно
    window_to_date = today                                    # исключительно (00:00 сегодня)
    last_included_date = today - timedelta(days=1)            # включительно (для ERP по дате)

    from_dt = datetime(
        window_from_date.year, window_from_date.month, window_from_date.day, tzinfo=_TZ_LOCAL
    )
    to_dt = datetime(
        window_to_date.year, window_to_date.month, window_to_date.day, tzinfo=_TZ_LOCAL
    )
    return {
        "window_from": window_from_date.isoformat(),
        "window_to": window_to_date.isoformat(),
        "cdp_date_from": window_from_date,
        "cdp_date_to": last_included_date,
        # amo_to_ts исключает window_to 00:00: лид ровно на верхней границе НЕ входит.
        "amo_from_ts": int(from_dt.timestamp()),
        "amo_to_ts": int(to_dt.timestamp()) - 1,
    }


def _load_lead_cache() -> dict:
    """Дисковый кеш точечных lookup'ов (2 ярус): {lead_id_str: ad_id|None}.

    None — «точечно проверили, у лида нет fb_ad_id / лид не наш» (кеш промаха,
    чтобы не передёргивать AMO повторно). Персистентный между прогонами крона."""
    return state_store.load_json_state(_LEAD_CACHE_FILE)


def _save_lead_cache(cache: dict) -> None:
    """Атомарно сохраняет дисковый кеш точечных lookup'ов."""
    state_store.save_json_state(_LEAD_CACHE_FILE, cache)


def _resolve_contract_numbers_pointed(missing_lead_ids: set[str], ad_ids: set[str]) -> dict[str, str]:
    """Точечно резолвит lead_id→ad_id для contract_number, не покрытых пакетным
    окном (2 ярус маппинга, см. докстринг модуля).

    Порядок:
      1. Кеш-хит (найденный ИЛИ null) — не зовём AMO повторно.
      2. Кеш-промах → integrations.amo.get_lead(int(lead_id)) точечно, с
         троттлингом _POINTED_LOOKUP_THROTTLE_SEC между вызовами и потолком
         _POINTED_LOOKUP_CAP запросов за прогон (защита AMO от перегрузки).
      3. Результат (ad_id или None) кешируется на диск сразу — переживает прогон
         и падение процесса (state_store пишет атомарно на каждую итерацию).
      4. Свыше потолка — оставшиеся lead_id логируются и остаются unmapped
         (fail-closed: не блокируем прогон, просто недосчитываем часть платежей).

    Args:
        missing_lead_ids: contract_number (как str) платежей, для которых
            пакетное окно НЕ дало ad_id.
        ad_ids: known_ad_ids из creative_kb — валидация «объявление наше».

    Returns:
        {lead_id_str: ad_id} только для успешно резолвленных (null-кеш и
        промахи в результат не попадают).
    """
    from integrations.amo import _extract_fb_fields, get_lead

    if not missing_lead_ids:
        return {}

    cache = _load_lead_cache()
    resolved: dict[str, str] = {}
    lookups_done = 0

    for lead_id_str in sorted(missing_lead_ids):
        # Кеш-хит (найденный ad_id ИЛИ null-промах) — не дёргаем AMO.
        if lead_id_str in cache:
            cached_ad_id = cache[lead_id_str]
            if cached_ad_id:
                resolved[lead_id_str] = cached_ad_id
            continue

        if lookups_done >= _POINTED_LOOKUP_CAP:
            logger.warning(
                "_resolve_contract_numbers_pointed: потолок %d точечных lookup достигнут, "
                "%d lead_id остались unmapped в этом прогоне",
                _POINTED_LOOKUP_CAP, len(missing_lead_ids) - lookups_done,
            )
            break

        if lookups_done > 0:
            time.sleep(_POINTED_LOOKUP_THROTTLE_SEC)  # троттлинг AMO между точечными запросами

        try:
            lead_id_int = int(lead_id_str)
            lead = get_lead(lead_id_int)
        except Exception as exc:
            logger.warning("_resolve_contract_numbers_pointed: get_lead(%s) ошибка — %s", lead_id_str, exc)
            lookups_done += 1
            continue

        lookups_done += 1

        if lead is None:
            cache[lead_id_str] = None  # null-кеш: лид не найден в AMO
            continue

        # get_lead возвращает RAW лид (custom_fields_values), _extract_fb_fields
        # ожидает ключ "custom_fields" — приводим как в get_leads_window.
        proxy = {"custom_fields": lead.get("custom_fields_values") or []}
        fb = _extract_fb_fields(proxy)
        ad_id = fb.get("ad_id")
        if ad_id:
            ad_id = str(ad_id).strip()
        if ad_id and ad_id in ad_ids:
            cache[lead_id_str] = ad_id
            resolved[lead_id_str] = ad_id
        else:
            cache[lead_id_str] = None  # нет fb_ad_id либо чужое объявление — null-кеш

    _save_lead_cache(cache)
    return resolved


def _build_deal_to_ad_map(
    ad_ids: set[str],
    contract_numbers: set[str] | None = None,
) -> dict[str, str]:
    """Строит {lead_id_str: ad_id} для сопоставления платежей ERP по contract_number.

    Источник маппинга — AMO: у лида есть fb_ad_id (902422), а lead["id"] — это
    тот же ID, что лежит в payment["contract_number"] у платежа ERP (см.
    докстринг модуля: deal_id платежа — внутренний ключ CDP, НЕ AMO; связь с
    AMO — только через contract_number). Функция называется по историческому
    имени "deal_to_ad_map" (сохранена сигнатура из спеки), но ключ результата —
    это lead_id/contract_number, а не CDP deal_id.

    ДВУХЪЯРУСНО:
      Ярус 1 — пакетное окно integrations.amo.get_leads_window (последние
        _DEAL_MAP_LOOKBACK_DAYS дней), как раньше — дёшево, без доп. нагрузки.
      Ярус 2 — если передан contract_numbers, для тех lead_id из него, что НЕ
        нашлись в ярусе 1, — точечный fallback _resolve_contract_numbers_pointed
        (см. её докстринг): дисковый кеш + get_lead с потолком/троттлингом.
        Покрывает случай «оплата в окне отчёта ← сделка вне окна get_leads_window»
        (лид создан раньше пакетного окна, платёж пришёл позже).

    Args:
        ad_ids: множество ad_id из creative_kb (для валидации, что объявление наше).
        contract_numbers: множество contract_number (str) из платежей текущего
            прогона — используется ТОЛЬКО для яруса 2 (точечный дозапрос
            непокрытых). None/пусто → только ярус 1 (обратная совместимость).

    Returns:
        {lead_id_str: ad_id}. Лиды без fb_ad_id или с чужим ad_id — пропускаются.
    """
    from integrations.amo import _extract_fb_fields, get_leads_window

    if not ad_ids:
        return {}

    now = datetime.now(tz=timezone.utc)
    from_ts = int((now - timedelta(days=_DEAL_MAP_LOOKBACK_DAYS)).timestamp())
    to_ts = int(now.timestamp())

    try:
        leads = get_leads_window(from_ts, to_ts)
    except Exception as exc:
        # AMO недоступна — ярус 1 пуст. Ярус 2 всё равно пробуем (get_lead может
        # быть доступен даже если пагинация списком временно упала) — но обычно
        # оба используют один и тот же AMO-клиент, так что тоже пусто.
        logger.warning("_build_deal_to_ad_map: AMO недоступна (ярус 1) — %s", exc)
        leads = []

    deal_map: dict[str, str] = {}
    for lead in leads:
        fb = _extract_fb_fields(lead)
        ad_id = fb.get("ad_id")
        lead_id = lead.get("id")
        if not ad_id or lead_id is None:
            continue
        ad_id = str(ad_id).strip()
        if ad_id in ad_ids:  # только НАШИ объявления (creative_kb)
            deal_map[str(lead_id)] = ad_id

    if contract_numbers:
        missing = {cn for cn in contract_numbers if cn not in deal_map}
        if missing:
            pointed = _resolve_contract_numbers_pointed(missing, ad_ids)
            deal_map.update(pointed)

    return deal_map


def compute_ad_payments_erp(
    date_from: date,
    date_to: date,
    *,
    payments: list[dict] | None = None,
) -> dict[str, dict]:
    """Агрегирует платежи ERP по объявлению за окно [date_from, date_to].

    Алгоритм:
      1. payments = cdp_client.get_payments(date_from, date_to)  # income+refund, окно honored.
         CdpError → {} сразу (fail-closed, AMO-запросы ниже не нужны).
      2. Собираем множество contract_number из платежей (для яруса 2 маппинга).
      3. deal_map = _build_deal_to_ad_map(known_ad_ids, contract_numbers) — ярус 1
         (пакетное окно) + ярус 2 (точечный fallback для непокрытых contract_number,
         см. докстринг _build_deal_to_ad_map).
      4. Дедуп по payment_id (одна запись платежа — один раз).
      5. НЕТТО ПО СДЕЛКЕ (contract_number), НЕ по документу: get_payments
         возвращает ПЛАТЁЖНЫЕ ДОКУМЕНТЫ, а не сделки. Рассрочка банка-партнёра
         генерит отдельный документ на каждый месяц — одна
         оплатившая сделка превращается в N документов, что завышало payments_erp
         как счётчик "сколько сделок оплатило" (Страж/скейлер сравнивают именно
         сделки, не документы). Поэтому: для каждой сделки (contract_number)
         суммируем net = Σincome − Σrefund по ВСЕМ её документам в окне; сделка
         считается оплаченной (+1 к payments_erp объявления), если net > 0.
         revenue_erp_lcy объявления — сумма net по ВСЕМ его сделкам (в т.ч. net<=0 —
         честная выручка окна, включая сделки, съеденные рефандом в ноль/минус).
      6. Возврат {ad_id: {"payments_erp": int, "revenue_erp_lcy": float}}.

    Fail-closed: CdpError от get_payments → logger.warning + возврат {} (пустой
    агрегат; вызывающий НЕ затирает AMO-поля). Исключение не пробрасывается наружу.

    Args:
        date_from: начало окна (включительно, по doc_date).
        date_to: конец окна (включительно, по doc_date).
        payments: уже полученный список платежей CDP (например, из
            refresh_payments_erp_7d, который сам ловит CdpError для fail-closed
            детекции полноты). None → функция сама зовёт cdp_client.get_payments
            (старое поведение, backward-compatible).

    Returns:
        {ad_id: {"payments_erp": int, "revenue_erp_lcy": float}}. Пустой — валиден.
        payments_erp — число УНИКАЛЬНЫХ сделок с net>0 (не число документов).
    """
    from services.creative_intelligence import _get_connection

    try:
        conn = _get_connection()
        try:
            rows = conn.execute("SELECT ad_id FROM creative_kb WHERE ad_id != ''").fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("compute_ad_payments_erp: KB недоступна — %s", exc)
        return {}

    known_ad_ids = {r["ad_id"] for r in rows}
    if not known_ad_ids:
        return {}

    if payments is None:
        try:
            payments = cdp_client.get_payments(date_from, date_to)  # income+refund
        except CdpError as exc:
            # Текст исключения CdpError уже маскирует ключ (см. cdp_client._request) —
            # повторная маскировка здесь не нужна.
            logger.warning("compute_ad_payments_erp: CDP недоступен — %s", exc)
            return {}  # fail-closed

    # Собираем contract_number ДО построения deal_map — второй ярус маппинга
    # (точечный fallback) дозапрашивает AMO только для тех contract_number,
    # что реально встретились в платежах этого окна (не тянем лишнее).
    contract_numbers: set[str] = set()
    for payment in payments:
        try:
            contract_numbers.add(str(int(payment.get("contract_number"))))
        except (TypeError, ValueError):
            continue  # нечисловой/пустой — всё равно будет пропущен ниже

    deal_map = _build_deal_to_ad_map(known_ad_ids, contract_numbers)
    if not deal_map:
        # AMO лежит (оба яруса) или нет ни одного сопоставленного лида — платежи
        # некому приписать.
        return {}

    # Шаг 1: нетто ПО СДЕЛКЕ (contract_number) — суммируем все её документы
    # (income плюс, refund минус), дедуп по payment_id (документа) внутри сделки.
    deal_net: dict[str, float] = {}
    deal_ad: dict[str, str] = {}  # lead_id -> ad_id (только сопоставленные)
    seen_ids: set = set()
    for payment in payments:
        payment_id = payment.get("id")
        if payment_id is not None:
            if payment_id in seen_ids:
                continue  # дедуп: один payment_id (документ) — один раз
            seen_ids.add(payment_id)

        # Ключ маппинга — contract_number (= AMO lead_id), НЕ deal_id (внутренний
        # surrogate-ключ CDP, см. докстринг модуля).
        # deal_id платежа сюда намеренно не используется — "мусорное" поле.
        contract_number = payment.get("contract_number")
        try:
            lead_id = str(int(contract_number))
        except (TypeError, ValueError):
            continue  # нечисловой/пустой contract_number — платёж не сопоставляем
        ad_id = deal_map.get(lead_id)
        if not ad_id:
            continue  # сделка не привязана к нашему объявлению / не FB — пропуск

        amount = float(payment.get("amount") or 0)
        direction = payment.get("direction")
        net = deal_net.get(lead_id, 0.0)
        if direction == "income":
            net += amount
        elif direction == "refund":
            net -= amount
        deal_net[lead_id] = net
        deal_ad[lead_id] = ad_id

    # Шаг 2: агрегируем на объявление — payments_erp = число сделок с net>0
    # (не число документов), revenue_erp_lcy = сумма net по всем сделкам объявления.
    agg: dict[str, dict] = {}
    for lead_id, net in deal_net.items():
        ad_id = deal_ad[lead_id]
        rec = agg.setdefault(ad_id, {"payments_erp": 0, "revenue_erp_lcy": 0.0})
        rec["revenue_erp_lcy"] += net
        if net > 0:
            rec["payments_erp"] += 1

    return agg


def refresh_creative_kb_payments_erp(months_back: int = 3) -> dict:
    """Считает ERP-агрегат за окно и пишет в creative_kb (только совпавшие ad_id).

    Окно: [today - months_back*30 дней, today]. State-файл хранит last_synced_doc_date
    (справочно/для отчётности; полный пересчёт окна дешёвый — платежи за 90 дней).

    Пишет columns payments_erp / revenue_erp_lcy / payments_erp_synced_at=datetime('now')
    ТОЛЬКО для ad_id, попавших в агрегат. Объявления без ERP-платежей НЕ трогаются
    (их payments_erp остаётся NULL = «нет данных ERP», не «точно 0» — важно для §6.4).

    Fail-closed: пустой агрегат (CDP лёг) → 0 обновлений, AMO-поля целы, warning.

    Returns:
        {"window_from": iso, "window_to": iso, "ads_updated": int,
         "payments_total": int, "revenue_total_lcy": float, "error": str | None}.
    """
    from services.creative_intelligence import _get_connection

    today = datetime.now(tz=timezone.utc).date()
    window_from = today - timedelta(days=months_back * 30)
    window_to = today

    agg = compute_ad_payments_erp(window_from, window_to)

    result: dict = {
        "window_from": window_from.isoformat(),
        "window_to": window_to.isoformat(),
        "ads_updated": 0,
        "payments_total": 0,
        "revenue_total_lcy": 0.0,
        "error": None,
    }

    if not agg:
        # Пустой агрегат — либо реально нет ERP-платежей за окно, либо CDP/AMO лёг
        # (compute_ad_payments_erp уже залогировал причину). AMO-поля не трогаем.
        result["error"] = "Нет данных ERP за окно (CDP/AMO недоступны либо платежей нет)"
        logger.warning("refresh_creative_kb_payments_erp: пустой агрегат, KB не изменена")
        return result

    try:
        conn = _get_connection()
    except Exception as exc:
        logger.warning("refresh_creative_kb_payments_erp: KB недоступна — %s", exc)
        result["error"] = f"KB недоступна: {exc}"
        return result

    try:
        updated = 0
        payments_total = 0
        revenue_total = 0.0
        for ad_id, m in agg.items():
            conn.execute(
                """
                UPDATE creative_kb
                SET
                    payments_erp            = ?,
                    revenue_erp_lcy          = ?,
                    payments_erp_synced_at   = datetime('now')
                WHERE ad_id = ?
                """,
                (m["payments_erp"], m["revenue_erp_lcy"], ad_id),
            )
            updated += 1
            payments_total += m["payments_erp"]
            revenue_total += m["revenue_erp_lcy"]
        conn.commit()
    except Exception as exc:
        logger.warning("refresh_creative_kb_payments_erp: ошибка записи в KB — %s", exc)
        result["error"] = f"Ошибка записи в KB: {exc}"
        return result
    finally:
        conn.close()

    result["ads_updated"] = updated
    result["payments_total"] = payments_total
    result["revenue_total_lcy"] = revenue_total

    # State — справочно (last_synced_doc_date = конец окна синка); полный
    # пересчёт окна дешёвый (90 дней платежей), state не используется для
    # инкрементальной догрузки, только для отчётности о последнем прогоне.
    state = _load_state()
    state["last_synced_doc_date"] = window_to.isoformat()
    state["last_run_at"] = datetime.now(tz=timezone.utc).isoformat()
    _save_state(state)

    return result


def refresh_payments_erp_7d(now: datetime | None = None) -> dict:
    """ПОЛНЫЙ refresh source-specific ERP-оплат РОВНО за 7 календарных дней (Wave 3A).

    Отличие от refresh_creative_kb_payments_erp (long-window, ≈3 мес): узкое честное
    окно ровно 7 суток (локальный TZ) и ТРАНЗАКЦИОННАЯ полная запись по ВСЕМ известным
    объявлениям с признаком полноты. Пишет колонки миграции 015:
    payments_erp_7d/revenue_erp_7d/erp_7d_window_from/erp_7d_window_to/erp_7d_synced_at/
    erp_7d_complete. НЕ трогает long-window payments_erp/revenue_erp_lcy.

    Семантика полноты (fail-closed):
      - CdpError (сеть/HTTP/пагинация/парсинг — cdp_client.get_payments) → НЕ
        помечаем complete, НЕ трогаем прошлый валидный снимок, synced_at не двигаем.
      - Платежи есть, но НИ ОДИН не сопоставлен объявлению (agg пуст) → трактуем как
        неполную атрибуцию (AMO недоступна / все не-FB) и fail-closed: НЕ complete.
        (Консервативно: лучше «нет свежих данных», чем ложный ноль по победителю.)
      - Платежей в окне нет (пустой список) → это ПОЛНЫЙ успешный refresh: все
        известные объявления получают 0 + complete=1 (подтверждённый ноль окна).
      - Успех: транзакционно для ВСЕХ известных ad_id — payments_erp_7d = агрегат
        (0 если платежей по объявлению нет) + complete=1 + окно/synced_at. Ошибка
        транзакции → rollback (нет смеси старых/новых строк), НЕ complete.

    Returns:
        {"window_from": iso, "window_to": iso, "ads_updated": int,
         "payments_total": int, "revenue_total_lcy": float,
         "complete": bool, "error": str | None}.
    """
    from services.creative_intelligence import _get_connection

    win = seven_day_window_local(now)
    synced_at = datetime.now(tz=timezone.utc).isoformat()
    result: dict = {
        "window_from": win["window_from"],
        "window_to": win["window_to"],
        "ads_updated": 0,
        "payments_total": 0,
        "revenue_total_lcy": 0.0,
        "complete": False,
        "error": None,
    }

    # Все известные объявления (полный refresh пишет им всем, даже с 0 оплат).
    try:
        conn = _get_connection()
        try:
            rows = conn.execute("SELECT ad_id FROM creative_kb WHERE ad_id != ''").fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("refresh_payments_erp_7d: KB недоступна — %s", exc)
        result["error"] = f"KB недоступна: {exc}"
        return result

    known_ad_ids = [r["ad_id"] for r in rows]
    if not known_ad_ids:
        result["error"] = "нет известных объявлений в creative_kb"
        return result

    # 1) CDP — единственный ЗАПРОС платежей (fail-closed по CdpError). Пустой список
    #    валиден (все нули). Ловим здесь, чтобы отличить «CDP лёг» от «платежей нет».
    try:
        payments = cdp_client.get_payments(win["cdp_date_from"], win["cdp_date_to"])
    except CdpError as exc:
        # Текст CdpError уже маскирует ключ (cdp_client._request).
        logger.warning("refresh_payments_erp_7d: CDP недоступен — %s (fail-closed, снимок не тронут)", exc)
        result["error"] = f"CDP недоступен: {exc}"
        return result

    # 2) Атрибуция платежей на объявления (net-по-сделке) — переиспользуем
    #    протестированную compute_ad_payments_erp с УЖЕ полученным списком (без
    #    повторного запроса к CDP).
    agg = compute_ad_payments_erp(win["cdp_date_from"], win["cdp_date_to"], payments=payments)

    if payments and not agg:
        # Платежи в окне есть, но ни один не привязан к объявлению — вероятна
        # недоступность AMO (маппинг lead→ad) или все платежи не-FB. Не можем
        # гарантировать полноту атрибуции → fail-closed (снимок не помечаем полным).
        logger.warning(
            "refresh_payments_erp_7d: %d платежей в окне, но 0 сопоставлено объявлениям — "
            "неполная атрибуция (AMO?), fail-closed", len(payments),
        )
        result["error"] = "ERP-платежи есть, но не сопоставлены (AMO недоступна?) — снимок неполный"
        return result

    # 3) Транзакционная ПОЛНАЯ запись: все известные ad_id получают значение
    #    (0 если оплат по объявлению нет) + complete=1 + границы окна + synced_at.
    try:
        conn = _get_connection()
    except Exception as exc:
        logger.warning("refresh_payments_erp_7d: KB недоступна при записи — %s", exc)
        result["error"] = f"KB недоступна: {exc}"
        return result

    updated = 0
    payments_total = 0
    revenue_total = 0.0
    try:
        conn.execute("BEGIN")
        for ad_id in known_ad_ids:
            m = agg.get(ad_id) or {}
            p = int(m.get("payments_erp", 0) or 0)
            r = float(m.get("revenue_erp_lcy", 0.0) or 0.0)
            conn.execute(
                """
                UPDATE creative_kb
                SET
                    payments_erp_7d    = ?,
                    revenue_erp_7d     = ?,
                    erp_7d_window_from = ?,
                    erp_7d_window_to   = ?,
                    erp_7d_synced_at   = ?,
                    erp_7d_complete    = 1
                WHERE ad_id = ?
                """,
                (p, r, win["window_from"], win["window_to"], synced_at, ad_id),
            )
            updated += 1
            payments_total += p
            revenue_total += r
        conn.commit()
    except Exception as exc:
        # Любая ошибка на середине → rollback: НЕ оставляем смесь старых/новых строк.
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("refresh_payments_erp_7d: ошибка записи, откат — %s", exc)
        result["error"] = f"Ошибка записи в KB (откат): {exc}"
        return result
    finally:
        conn.close()

    result["ads_updated"] = updated
    result["payments_total"] = payments_total
    result["revenue_total_lcy"] = revenue_total
    result["complete"] = True
    return result


def payments_effective(ad: dict) -> int | None:
    """Объединённый сигнал оплат по правилам источника-каскада (§6.4).

    Семантика ЗАФИКСИРОВАНА (fail-closed): сигнал оплаты не теряется.
      - payments_amo = ad.get("payments")          # None = не сверено AMO
      - payments_erp  = ad.get("payments_erp")        # None = не сверено ERP
      - Оба None → None («нет данных ни от кого» — не паузим как слив).
      - Один None → берём непустой.
      - Оба не None → max(payments_amo, payments_erp)  # лучше ложно-позитив, чем пропуск.

    Обоснование max: этот сигнал защищает победителя от паузы (confirmed_waster
    требует payments==0). Ложно-положительная оплата → в худшем случае НЕ запаузили
    слабое объявление (деньги в пределах дневного капа/потолков пилота). Ложно-
    отрицательная (пропущенная) оплата → запаузили ПОБЕДИТЕЛЯ с реальными деньгами
    — необратимая потеря обучения FB. Асимметрия рисков → max.

    Возвращает int | None.
    """
    payments_amo = ad.get("payments")
    payments_erp = ad.get("payments_erp")

    if payments_amo is None and payments_erp is None:
        return None
    if payments_amo is None:
        return max(0, int(payments_erp))
    if payments_erp is None:
        return max(0, int(payments_amo))
    return max(int(payments_amo), int(payments_erp))


# ---------------------------------------------------------------------------
# Чтение честного 7d-снимка: свежесть + полнота (Wave 3A, для budget_scaler)
# ---------------------------------------------------------------------------

def _snapshot_fresh_complete(ad: dict, prefix: str, now: datetime | None = None) -> bool:
    """True если source-specific 7d-снимок объявления СВЕЖИЙ И ПОЛНЫЙ.

    prefix — 'amo' или 'erp' (префиксы колонок AMO- и ERP-снимка миграции 015). Свежесть:
    границы снимка (window_from/window_to) совпадают с ОЖИДАЕМЫМ 7d-окном для now
    (значит refresh отработал в этом цикле). Полнота: <prefix>_7d_complete == 1.
    NULL/0/несовпадение окна → False (нет подтверждённых полных данных).
    """
    if not ad.get(f"{prefix}_7d_complete"):
        return False
    win = seven_day_window_local(now)
    return (
        ad.get(f"{prefix}_7d_window_from") == win["window_from"]
        and ad.get(f"{prefix}_7d_window_to") == win["window_to"]
    )


def is_amo_7d_fresh_complete(ad: dict, now: datetime | None = None) -> bool:
    """AMO 7d-снимок объявления свежий и полный (см. _snapshot_fresh_complete)."""
    return _snapshot_fresh_complete(ad, "amo", now)


def is_erp_7d_fresh_complete(ad: dict, now: datetime | None = None) -> bool:
    """ERP 7d-снимок объявления свежий и полный (см. _snapshot_fresh_complete)."""
    return _snapshot_fresh_complete(ad, "erp", now)


def payments_7d_effective(
    ad: dict, payments_source: str, now: datetime | None = None
) -> int | None:
    """Эффективный сигнал оплат за 7 дней ТОЛЬКО среди свежих полных источников.

    Семантика (Wave 3A, зеркалит payments_effective, но для честного 7d-окна и с
    учётом свежести/полноты каждого источника):
      - amo/shadow → берём AMO 7d, если он свежий полный; иначе None (нет данных).
        (В shadow-режиме ERP 7d только сравнивается/логируется — на сигнал не влияет.)
      - erp → max(AMO 7d, ERP 7d) СРЕДИ свежих полных источников; ни одного свежего
        полного → None (fail-closed — «нет подтверждённого 7d-сигнала»).
    None ≠ 0: означает «нет подтверждённых полных 7d-данных», НЕ «оплат ноль».
    """
    amo_ok = is_amo_7d_fresh_complete(ad, now)
    amo_val = int(ad.get("payments_amo_7d") or 0) if amo_ok else None

    if payments_source != "erp":
        return amo_val

    erp_ok = is_erp_7d_fresh_complete(ad, now)
    erp_val = int(ad.get("payments_erp_7d") or 0) if erp_ok else None

    fresh = [v for v in (amo_val, erp_val) if v is not None]
    if not fresh:
        return None  # fail-closed: нет ни одного свежего полного источника
    return max(fresh)


def seven_d_window_confirmed(
    ads: list[dict], payments_source: str, now: datetime | None = None
) -> bool:
    """True если честное 7d-окно ПОДТВЕРЖДЕНО для требуемого источника (run-level).

    Снимок — свойство прогона refresh: успешный полный refresh штампует ВСЕ строки
    creative_kb одинаковыми границами/complete. Поэтому «окно подтверждено» =
    существует хотя бы одно объявление со свежим полным снимком нужного источника:
      - amo/shadow → нужен свежий полный AMO 7d.
      - erp → достаточно свежего полного AMO 7d ИЛИ ERP 7d (max среди свежих; §6.5).
    Пустой список / нет 7d-данных → False (по умолчанию честного окна нет).
    Используется скейлером для (а) правдивой подписи «7 дней» и (б) fail-closed
    гейта active-подъёма (флаг scaler_v2.require_fresh_7d).
    """
    for ad in ads:
        if is_amo_7d_fresh_complete(ad, now):
            return True
        if payments_source == "erp" and is_erp_7d_fresh_complete(ad, now):
            return True
    return False


def _load_state() -> dict:
    """state_store.load_json_state(_STATE_FILE) с дефолтом {}."""
    return state_store.load_json_state(_STATE_FILE)


def _save_state(state: dict) -> None:
    """state_store.save_json_state(_STATE_FILE, state) — атомарно."""
    state_store.save_json_state(_STATE_FILE, state)
