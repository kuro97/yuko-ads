"""
AMO CRM интеграция — выгрузка лидов и матчинг к рекламам.

Алгоритм:
1. OAuth2 авторизация (refresh_token → access_token)
2. Получение лидов с UTM-метками и FB полями за период
3. Матчинг ТОЛЬКО по точному совпадению fb_ad_name → ad_id (фоллбэков по adset/campaign нет)
4. Классификация лидов: новый / квал / оплата / отказ
5. Расчёт метрик: qual_pct, cpql, romi, revenue
"""

import contextlib
import contextvars
import json
import logging
import os
import tempfile
import time
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    AMO_DOMAIN, AMO_CLIENT_ID, AMO_CLIENT_SECRET, AMO_REFRESH_TOKEN,
    AMO_PIPELINE_ID, AMO_PAYMENT_STATUS_IDS,
    AMO_FB_LEAD_ID_FIELD,
    MEETING_SCHEDULED_STATUS_IDS, MEETING_HELD_STATUS_IDS,
)
from services.sources import _get_tags_lower

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
TOKEN_FILE = DATA_DIR / "amo_token.json"

# Connection pooling — переиспользуем TCP соединения
session = requests.Session()


# --- Бюджет чтений (опционально, для путей с дедлайном) ---
#
# По умолчанию AMO читается «как раньше»: connect=10 / read=60, 3 попытки. Это
# правильно для ночных импортов, но смертельно для исполнения одобренного
# владельцем действия: одна зависшая страница держит поток до 3.5 минут, а таких
# чтений в одном задании несколько. Кто работает под дедлайном — оборачивает свои
# вызовы в request_budget(...) и получает короткие таймауты, одну попытку и
# жёсткий срок: просроченный бюджет сразу даёт Timeout, а не новый запрос.


@dataclass(frozen=True, slots=True)
class AmoRequestBudget:
    connect_seconds: float
    read_seconds: float
    attempts: int
    deadline_monotonic: float | None


_REQUEST_BUDGET: contextvars.ContextVar[AmoRequestBudget | None] = contextvars.ContextVar(
    "amo_request_budget",
    default=None,
)


@contextlib.contextmanager
def request_budget(
    *,
    connect_seconds: float = 5.0,
    read_seconds: float = 20.0,
    attempts: int = 1,
    deadline_monotonic: float | None = None,
):
    """Ограничивает AMO-чтения внутри блока. Снаружи поведение не меняется."""

    if attempts < 1:
        raise ValueError("attempts должен быть >= 1")
    token = _REQUEST_BUDGET.set(
        AmoRequestBudget(
            connect_seconds=connect_seconds,
            read_seconds=read_seconds,
            attempts=attempts,
            deadline_monotonic=deadline_monotonic,
        )
    )
    try:
        yield
    finally:
        _REQUEST_BUDGET.reset(token)


# --- OAuth2 ---

def _load_token() -> dict:
    """Загружает сохранённый токен.

    Битый / пустой / отсутствующий файл -> {} (лог warning), чтобы сработал
    задуманный фолбэк на AMO_REFRESH_TOKEN из env в get_access_token(), а не
    падали все AMO-кроны на JSONDecodeError.
    """
    if not TOKEN_FILE.exists():
        return {}
    try:
        raw = TOKEN_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("AMO: не удалось прочитать файл токена %s (%s) — фолбэк на env", TOKEN_FILE, exc)
        return {}
    # Файл есть, но пустой (например, обрыв записи старой неатомарной версией)
    if not raw.strip():
        logger.warning("AMO: файл токена %s пустой — фолбэк на env", TOKEN_FILE)
        return {}
    try:
        return json.loads(raw)
    except ValueError as exc:  # json.JSONDecodeError — подкласс ValueError
        logger.warning("AMO: битый JSON в файле токена %s (%s) — фолбэк на env", TOKEN_FILE, exc)
        return {}


def _save_token(token_data: dict):
    """Атомарно сохраняет токен на диск.

    AMO ротирует одноразовый refresh_token — обрыв во время записи мог бы
    оставить обрезанный файл и убить всю связку с CRM. Пишем во временный
    файл в ТОЙ ЖЕ папке и атомарно подменяем (os.replace): читатель всегда
    видит либо старую, либо новую версию целиком, но никогда обрезанную.
    """
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(token_data, ensure_ascii=False)
    # Временный файл обязан лежать рядом с целевым — os.replace атомарен
    # только в пределах одной файловой системы.
    fd, tmp_path = tempfile.mkstemp(
        dir=str(TOKEN_FILE.parent), prefix=".amo_token.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, TOKEN_FILE)
    except BaseException:
        # Любой сбой записи/замены — убираем временный файл, оригинал не тронут
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_access_token() -> str:
    """Получает access_token, обновляя через refresh_token при необходимости."""
    token = _load_token()

    # Если токен свежий (менее 12 часов) — используем
    if token.get("access_token") and token.get("expires_at", 0) > datetime.now().timestamp():
        return token["access_token"]

    # Обновляем через refresh_token
    refresh = token.get("refresh_token") or AMO_REFRESH_TOKEN
    if not refresh:
        raise Exception("AMO: нет refresh_token. Заполни AMO_REFRESH_TOKEN в .env")

    # таймаут — иначе зависший AMO заморозит поток
    resp = session.post(
        f"https://{AMO_DOMAIN}.amocrm.ru/oauth2/access_token",
        json={
            "client_id": AMO_CLIENT_ID,
            "client_secret": AMO_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "redirect_uri": f"https://{AMO_DOMAIN}.amocrm.ru",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise Exception(f"AMO OAuth ошибка: {resp.text}")

    data = resp.json()
    token_data = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": datetime.now().timestamp() + data["expires_in"] - 60,
    }
    _save_token(token_data)
    return token_data["access_token"]


def _amo_get(endpoint: str, params: dict = None) -> dict:
    """GET запрос к AMO CRM API с ретраями на таймаут.

    AMO иногда отдаёт тяжёлые страницы (250 лидов с embed) дольше обычного —
    одна медленная страница не должна ронять весь импорт. GET идемпотентен,
    повтор безопасен: 3 попытки с нарастающей паузой, connect=10с / read=60с.

    Внутри request_budget(...) таймауты и число попыток берутся из бюджета, а
    истёкший дедлайн сразу даёт Timeout — это путь исполнения одобренных
    действий, где ждать зависший AMO минутами нельзя.
    """
    budget = _REQUEST_BUDGET.get()
    timeout = (
        (10, 60)
        if budget is None
        else (budget.connect_seconds, budget.read_seconds)
    )
    max_attempts = 3 if budget is None else budget.attempts
    token = get_access_token()
    url = f"https://{AMO_DOMAIN}.amocrm.ru/api/v4/{endpoint}"
    headers = {"Authorization": f"Bearer {token}"}
    last_err = None
    for attempt in range(max_attempts):
        if budget is not None and budget.deadline_monotonic is not None:
            # Срок вышел — новый запрос уже некуда положить, честнее сказать это
            # сразу, чем занять поток ещё на один таймаут.
            if time.monotonic() >= budget.deadline_monotonic:
                raise requests.exceptions.Timeout("AMO_REQUEST_BUDGET_EXPIRED")
        try:
            resp = session.get(url, headers=headers, params=params or {}, timeout=timeout)
            if resp.status_code == 204:
                return {"_embedded": {"leads": []}}
            if resp.status_code != 200:
                raise Exception(f"AMO API ошибка ({resp.status_code}): {resp.text[:200]}")
            return resp.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            if attempt < max_attempts - 1:
                logger.warning(
                    "AMO GET %s таймаут/обрыв (попытка %d/%d) — повтор",
                    endpoint,
                    attempt + 1,
                    max_attempts,
                )
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise last_err  # недостижимо, но для линтера


def _amo_patch(endpoint: str, json_body: dict | list) -> dict:
    """PATCH запрос к AMO CRM API.
    Принимает dict (для одного объекта) или list (для bulk-операций — AMO ожидает массив).
    encode('utf-8') обязателен для корректной передачи не-ASCII тегов (кириллица и т.п.).
    """
    token = get_access_token()
    resp = session.patch(
        f"https://{AMO_DOMAIN}.amocrm.ru/api/v4/{endpoint}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(json_body, ensure_ascii=False).encode("utf-8"),
        timeout=30,
    )
    if resp.status_code not in (200, 202, 204):
        raise Exception(f"AMO PATCH ошибка ({resp.status_code}) {endpoint}: {resp.text[:300]}")
    # 204 No Content — успех, но тело пустое
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def _amo_post(endpoint: str, json_body: dict | list) -> dict:
    """POST запрос к AMO CRM API (создание сущностей — задачи, заметки).
    Принимает dict или list (для bulk-операций — AMO ожидает массив).
    encode('utf-8') обязателен для корректной передачи не-ASCII текста (кириллица и т.п.).
    """
    token = get_access_token()
    resp = session.post(
        f"https://{AMO_DOMAIN}.amocrm.ru/api/v4/{endpoint}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(json_body, ensure_ascii=False).encode("utf-8"),
        timeout=30,
    )
    if resp.status_code not in (200, 201, 202, 204):
        raise Exception(f"AMO POST ошибка ({resp.status_code}) {endpoint}: {resp.text[:300]}")
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def get_lead(lead_id: int, with_params: list[str] = None) -> dict | None:
    """GET /leads/{id}?with=... — один лид с опциональными связями.
    Возвращает None если лид не найден (404).
    """
    token = get_access_token()
    params = {}
    if with_params:
        params["with"] = ",".join(with_params)
    resp = session.get(
        f"https://{AMO_DOMAIN}.amocrm.ru/api/v4/leads/{lead_id}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise Exception(f"AMO get_lead ошибка ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


def get_contact_with_leads(contact_id: int) -> dict | None:
    """GET /contacts/{id}?with=leads — контакт со списком привязанных лидов.
    Список lead_id лежит в _embedded.leads[].id.
    Возвращает None если контакт не найден (404).
    """
    token = get_access_token()
    resp = session.get(
        f"https://{AMO_DOMAIN}.amocrm.ru/api/v4/contacts/{contact_id}",
        headers={"Authorization": f"Bearer {token}"},
        params={"with": "leads"},
        timeout=30,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise Exception(f"AMO get_contact ошибка ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


def get_leads_batch(lead_ids: list[int]) -> list[dict]:
    """Получает несколько лидов одним запросом GET /leads?filter[id][]=...
    AMO ограничивает фильтр ~50 ID — батчим по 50.
    Возвращает плоский список RAW лидов (со всеми полями: custom_fields_values, _embedded.tags и т.д.).
    """
    result: list[dict] = []
    BATCH_SIZE = 50

    for i in range(0, len(lead_ids), BATCH_SIZE):
        chunk = lead_ids[i:i + BATCH_SIZE]
        # requests принимает список кортежей для повторяющихся ключей
        params = [("with", "contacts")]
        for lid in chunk:
            params.append(("filter[id][]", str(lid)))
        token = get_access_token()
        resp = session.get(
            f"https://{AMO_DOMAIN}.amocrm.ru/api/v4/leads",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        if resp.status_code == 204:
            # Нет лидов по этим ID — пропускаем батч
            continue
        if resp.status_code != 200:
            raise Exception(f"AMO get_leads_batch ошибка ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        result.extend(data.get("_embedded", {}).get("leads", []))

    return result


def get_contacts_lead_counts(contact_ids: list[int]) -> dict[int, int]:
    """Возвращает {contact_id: число привязанных лидов} батчами по 50.

    Использует GET /contacts?filter[id][]=...&with=leads — один запрос на ~50
    контактов. Нужно чтобы дёшево понять «есть ли у контакта история в CRM».
    """
    counts: dict[int, int] = {}
    for cid, lead_ids in get_contacts_with_leads_batch(contact_ids).items():
        counts[cid] = len(lead_ids)
    return counts


def get_contacts_with_leads_batch(contact_ids: list[int]) -> dict[int, list[int]]:
    """Возвращает {contact_id: [lead_id, ...]} батчами по 50.

    GET /contacts?filter[id][]=...&with=leads — нужно чтобы найти ВСЕ лиды
    контакта (историю), а потом догрузить их и определить реальный источник.
    """
    result: dict[int, list[int]] = {}
    ids = list(contact_ids)
    BATCH = 50  # AMO ограничивает filter[id][] ~50 значениями
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        params = [("with", "leads"), ("limit", 50)]
        for cid in chunk:
            params.append(("filter[id][]", str(cid)))
        data = _amo_get("contacts", params)
        for c in data.get("_embedded", {}).get("contacts", []):
            c_leads = (c.get("_embedded") or {}).get("leads") or []
            result[c["id"]] = [l["id"] for l in c_leads if l.get("id")]
    return result


# --- Получение лидов ---

def get_leads(days: int = 30) -> list[dict]:
    """Получает лиды из AMO CRM за последние N дней.
    Возвращает список с UTM, статусом и бюджетом."""
    date_from = int((datetime.now() - timedelta(days=days)).timestamp())
    all_leads = []
    page = 1

    while True:
        params = {
            "filter[created_at][from]": date_from,
            "with": "contacts,source_id",
            "limit": 250,
            "page": page,
        }
        # Фильтруем по воронке — иначе придут десятки тысяч лидов со всех воронок
        if AMO_PIPELINE_ID:
            params["filter[pipeline_id]"] = AMO_PIPELINE_ID
        data = _amo_get("leads", params)
        leads = data.get("_embedded", {}).get("leads", [])
        if not leads:
            break

        for lead in leads:
            all_leads.append({
                "id": lead["id"],
                "name": lead.get("name", ""),
                "status_id": lead.get("status_id"),
                "pipeline_id": lead.get("pipeline_id"),
                "price": lead.get("price", 0) or 0,
                "created_at": lead.get("created_at"),
                "contacts": (lead.get("_embedded") or {}).get("contacts") or [],
                "custom_fields": lead.get("custom_fields_values") or [],
                # Дополнительные поля для автокопирования источника
                "created_by": lead.get("created_by"),
                "responsible_user_id": lead.get("responsible_user_id"),
                "tags": (lead.get("_embedded") or {}).get("tags") or [],
                # Встроенное поле AMO (НЕ custom_field) — источник лида (телефония, WhatsApp, Бот и др.)
                "source_id": lead.get("source_id"),
            })

        # Пагинация
        if len(leads) < 250:
            break
        page += 1

    return all_leads


def get_leads_window(from_ts: int, to_ts: int) -> list[dict]:
    """Получает лиды из AMO CRM за историческое окно [from_ts, to_ts] (unix-время).

    Аналог get_leads, но фильтрует по created_at с двусторонними границами.
    Используется в services.amo_outcomes для порционного скачивания истории AMO.

    Пагинация: по 250 лидов за запрос (лимит AMO API).
    Троттлинг: встроен в _amo_get (переиспользует OAuth-токен + session pooling).
    Если AMO_PIPELINE_ID задан — фильтруем по воронке (иначе приходят лиды всех воронок).

    Returns: список лидов в том же формате что get_leads.
    """
    all_leads = []
    page = 1

    while True:
        params = {
            "filter[created_at][from]": from_ts,
            "filter[created_at][to]": to_ts,
            "with": "contacts,source_id",
            "limit": 250,
            "page": page,
        }
        # Фильтр по воронке — без него приходят лиды со всех воронок (десятки тысяч записей)
        if AMO_PIPELINE_ID:
            params["filter[pipeline_id]"] = AMO_PIPELINE_ID

        data = _amo_get("leads", params)
        leads = data.get("_embedded", {}).get("leads", [])
        if not leads:
            break

        for lead in leads:
            all_leads.append({
                "id": lead["id"],
                "name": lead.get("name", ""),
                "status_id": lead.get("status_id"),
                "pipeline_id": lead.get("pipeline_id"),
                "price": lead.get("price", 0) or 0,
                "created_at": lead.get("created_at"),
                "contacts": (lead.get("_embedded") or {}).get("contacts") or [],
                "custom_fields": lead.get("custom_fields_values") or [],
                # Дополнительные поля для автокопирования источника
                "created_by": lead.get("created_by"),
                "responsible_user_id": lead.get("responsible_user_id"),
                "tags": (lead.get("_embedded") or {}).get("tags") or [],
                # Встроенное поле AMO (НЕ custom_field) — источник лида (телефония, WhatsApp, Бот и др.)
                "source_id": lead.get("source_id"),
            })

        # Пагинация: если меньше 250 — это последняя страница
        if len(leads) < 250:
            break
        page += 1

    return all_leads


def get_latest_fb_lead_ts() -> int | None:
    """Возвращает unix-время (created_at) самого свежего реального FB Lead Ads лида в AMO.

    Пагинирует по 250 лидов (order created_at desc) до первого найденного FB-лида.
    Остановка:
      - найден лид с fb_lead_id → возвращаем его created_at
      - страница содержит лид старее 48 часов → стоп, возвращаем None
      - исчерпаны все 8 страниц → стоп, возвращаем None
      - список вернул < 250 лидов (последняя страница) → стоп
    Троттлинг: 0.2 с между страницами (кроме первой).
    None — если реально не нашли FB-лид за 48 ч или AMO недоступна.
    """
    # Граница по возрасту: лиды старше 48 часов не смотрим
    MAX_PAGES = 8
    CUTOFF_SECONDS = 48 * 3600
    THROTTLE_SEC = 0.2

    try:
        cutoff_ts = int(datetime.now().timestamp()) - CUTOFF_SECONDS

        for page in range(1, MAX_PAGES + 1):
            if page > 1:
                time.sleep(THROTTLE_SEC)

            params: dict = {
                "order[created_at]": "desc",
                "limit": 250,
                "page": page,
            }
            # Фильтруем по воронке если задана — иначе идут лиды со всех воронок
            if AMO_PIPELINE_ID:
                params["filter[pipeline_id]"] = AMO_PIPELINE_ID

            data = _amo_get("leads", params)
            leads = data.get("_embedded", {}).get("leads", [])

            if not leads:
                break

            for lead in leads:
                created_at = lead.get("created_at") or 0

                # Если лид старее 48 ч — вся страница ещё старее (сортировка desc), стоп
                if created_at < cutoff_ts:
                    return None

                # _extract_fb_lead_id ожидает ключ "custom_fields",
                # но в сыром ответе AMO поле называется "custom_fields_values".
                proxy = {"custom_fields": lead.get("custom_fields_values") or []}
                if _extract_fb_lead_id(proxy) is not None:
                    return created_at

            # Последняя страница — меньше 250 лидов
            if len(leads) < 250:
                break

        return None
    except Exception as exc:
        logger.warning("get_latest_fb_lead_ts: ошибка при запросе к AMO — %s", exc)
        return None


def _extract_utm(lead: dict) -> dict:
    """Извлекает UTM-метки из кастомных полей лида."""
    utm = {}
    for field in lead.get("custom_fields", []):
        name = (field.get("field_name") or "").lower()
        values = field.get("values", [])
        value = values[0]["value"] if values else None
        if not value:
            continue
        if "utm_source" in name:
            utm["utm_source"] = value
        elif "utm_medium" in name:
            utm["utm_medium"] = value
        elif "utm_campaign" in name:
            utm["utm_campaign"] = value
        elif "utm_content" in name:
            utm["utm_content"] = value
        elif "utm_term" in name:
            utm["utm_term"] = value
    return utm


def _extract_fb_fields(lead: dict) -> dict:
    """Извлекает FB поля из кастомных полей лида AMO.
    Поля: fb_ad_name, fb_adset_name, fb_campaign_name, fb_ad_id.

    fb_ad_id (field_name содержит 'fb_ad_id', field_id=902422) — точный числовой ID объявления.
    Возвращается строкой для единообразия с ad_id в creative_kb.
    Запасной вариант: поиск по field_id=902422 если field_name не сработал.
    """
    fb = {}
    # Первый проход: читаем все поля по field_name
    for field in lead.get("custom_fields", []):
        name = (field.get("field_name") or "").lower()
        values = field.get("values", [])
        value = values[0]["value"] if values else None
        if not value:
            continue
        # Порядок важен: fb_ad_name содержит 'fb_ad_name', поэтому проверяем fb_ad_id ПЕРВЫМ
        # чтобы он не попал в ветку fb_ad_name (имена 'fb_ad_id' содержит 'fb_ad_name' подстрокой? нет)
        # 'fb_ad_name' не является подстрокой 'fb_ad_id' — порядок не критичен, но явно разделяем
        if "fb_ad_id" in name:
            fb["ad_id"] = str(value).strip()
        elif "fb_ad_name" in name:
            fb["ad_name"] = value
        elif "fb_adset_name" in name:
            fb["adset_name"] = value
        elif "fb_campaign_name" in name:
            fb["campaign_name"] = value

    # Запасной вариант: если field_name не содержал 'fb_ad_id' — ищем по field_id=902422
    if "ad_id" not in fb:
        for field in lead.get("custom_fields", []):
            if field.get("field_id") == 902422:
                values = field.get("values", [])
                value = values[0]["value"] if values else None
                if value:
                    fb["ad_id"] = str(value).strip()
                break

    return fb


def _extract_fb_lead_id(lead: dict) -> int | None:
    """Извлекает Facebook Lead ID (leadgen_id) из кастомного поля AMO CRM.

    Имя поля задаётся в AMO_FB_LEAD_ID_FIELD (по умолчанию "FB Lead ID").
    Это число 15-17 цифр, НЕ внутренний ID AMO и НЕ ID формы.
    """
    target = AMO_FB_LEAD_ID_FIELD.lower().strip()
    for field in lead.get("custom_fields", []):
        name = (field.get("field_name") or "").lower().strip()
        if name == target:
            values = field.get("values", [])
            raw = values[0]["value"] if values else None
            if raw:
                try:
                    return int(raw)
                except (ValueError, TypeError):
                    return None
    return None


# --- Классификация ---

def _is_qualified(lead: dict) -> bool:
    """Проверяет поле 'Квалификация пройдена' = 'ДА'."""
    for field in lead.get("custom_fields", []):
        name = (field.get("field_name") or "")
        if name == "Квалификация пройдена":
            values = field.get("values", [])
            value = str(values[0]["value"] if values else "").upper().strip()
            return value == "ДА"
    return False


def classify_lead(lead: dict) -> str:
    """Классифицирует лид по кастомному полю 'Квалификация пройдена'.
    Возвращает: 'оплата', 'квал', 'новый'."""
    status_id = lead.get("status_id")
    if status_id in AMO_PAYMENT_STATUS_IDS:
        return "оплата"
    if _is_qualified(lead):
        return "квал"
    return "новый"


def _is_service_lead(lead: dict) -> bool:
    """True если лид служебный и НЕ должен считаться во встречах (ARCH-hold-meetings).

    Служебные признаки (регистронезависимо, по подстроке в имени тега):
      - тег содержит «автосделка» — авто-скопированная сделка AMO;
      - тег содержит «рассылка waba» — копия рассылки Waba (fb_lead_id завышается
        рассылкой — реальные FB-лиды только без «Автосделка»).
    Это защита от завышения счётчика встреч: рассылка Waba копирует лиды
    и может имитировать нахождение на этапе встречи.
    """
    tags = _get_tags_lower(lead)
    return any("автосделка" in t or "рассылка waba" in t for t in tags)


# --- Матчинг ---

def _build_fb_lookup(ads: list[dict]) -> dict:
    """Строит обратный индекс из FB объявлений ТОЛЬКО по имени объявления.
    Возвращает: {ad_name_lower: ad_id}.
    Намеренно НЕ включает adset_name и campaign_name — матчинг только точный по объявлению."""
    lookup = {}
    for ad in ads:
        ad_id = ad.get("id") or ad.get("ad_id")
        if not ad_id:
            continue
        # Берём только имя объявления (два возможных ключа: 'name' и 'ad_name')
        for key in ("name", "ad_name"):
            name = (ad.get(key) or "").strip().lower()
            if name and name not in lookup:
                lookup[name] = ad_id
    return lookup


def match_leads_to_ads(leads: list[dict], fb_lookup: dict = None, known_ad_ids: set = None) -> dict:
    """Группирует лиды по ad_id.

    Приоритет матчинга:
    1. По fb_ad_id (точный числовой ID объявления из поля лида) — если ad_id есть в known_ad_ids.
       Устраняет коллизию имён: два объявления с одинаковым именем разводятся по id.
    2. Fallback по fb_ad_name через fb_lookup — для старых лидов без fb_ad_id,
       и для лидов чей fb_ad_id не входит в known_ad_ids (объявление не в KB).

    Лиды без fb_ad_name И без fb_ad_id, или без совпадения в обоих матчерах — пропускаются.

    fb_lookup: {ad_name_lower: ad_id} — обратный индекс из FB по имени.
    known_ad_ids: множество ad_id из creative_kb — для валидации прямого матча по id.

    Возвращает: {ad_id: {leads: [...], total, quals, payments, revenue}}.

    ДЕДУП (фикс двойного счёта выручки):
    1. По lead_id — каждый лид засчитывается РОВНО ОДИН раз (защита от дублей AMO/окон).
    2. По contact_id — один контакт получает revenue/payments только за ПЕРВУЮ по времени
       оплаченную сделку. Так устраняется задвоение платежа у клиента с несколькими
       сделками на разные объявления (пример: один платёж 500000 на двух лидах).
       Детерминизм: лиды сортируются по created_at ASC, первая оплата контакта побеждает.
    """
    by_ad = {}
    fb_lookup = fb_lookup or {}
    known_ad_ids = known_ad_ids or set()
    seen_lead_ids: set = set()  # дедуп по lead_id — один лид считаем один раз

    # Сортируем лиды по created_at ASC для детерминизма дедупа по контакту.
    # Лиды без created_at уходят в конец (None → бесконечность).
    sorted_leads = sorted(leads, key=lambda l: (l.get("created_at") is None, l.get("created_at") or 0))

    # Дедуп по contact_id касается ТОЛЬКО revenue и payments.
    # total, quals — не трогаем: квалификация может быть у одного контакта несколько раз.
    seen_paying_contacts: set = set()

    for lead in sorted_leads:
        # Пропускаем повтор того же лида внутри вызова (защита от дублей AMO/окон)
        lead_id = lead.get("id")
        if lead_id is not None:
            if lead_id in seen_lead_ids:
                continue
            seen_lead_ids.add(lead_id)

        fb = _extract_fb_fields(lead)

        # Матчинг: сначала по точному fb_ad_id, затем fallback по имени
        ad_id = None

        # Приоритет 1: прямой матч по fb_ad_id — исключает коллизию имён
        lead_fb_ad_id = fb.get("ad_id")
        if lead_fb_ad_id and known_ad_ids and lead_fb_ad_id in known_ad_ids:
            ad_id = lead_fb_ad_id

        # Fallback: матч по имени объявления (для старых лидов без fb_ad_id,
        # или если fb_ad_id не входит в known_ad_ids)
        if not ad_id and fb.get("ad_name") and fb_lookup:
            ad_id = fb_lookup.get(fb["ad_name"].strip().lower())

        if not ad_id:
            continue

        if ad_id not in by_ad:
            by_ad[ad_id] = {"leads": [], "total": 0, "quals": 0, "payments": 0, "revenue": 0}

        status = classify_lead(lead)
        by_ad[ad_id]["leads"].append(lead)
        by_ad[ad_id]["total"] += 1

        if status in ("квал", "оплата"):
            by_ad[ad_id]["quals"] += 1

        if status == "оплата":
            # Извлекаем contact_id для дедупа по контакту.
            # contacts — список объектов [{id: ...}, ...], берём первый.
            contacts = lead.get("contacts") or []
            contact_id = contacts[0].get("id") if contacts else None

            if contact_id is not None:
                # Если этот контакт уже оплатил в другой сделке — не засчитываем
                if contact_id in seen_paying_contacts:
                    continue
                seen_paying_contacts.add(contact_id)

            # contact_id is None — нет данных о контакте, fallback на старое поведение
            by_ad[ad_id]["payments"] += 1
            by_ad[ad_id]["revenue"] += lead["price"]

    return by_ad


def count_meetings_by_ad(
    leads: list[dict],
    fb_lookup: dict | None = None,
    known_ad_ids: set | None = None,
) -> dict[str, dict]:
    """Считает лидов на этапах встреч по ad_id (для критерия удержания фазы 2, ARCH-hold-meetings).

    Матч тем же приоритетом что match_leads_to_ads:
      1) по fb_ad_id (fb["ad_id"] in known_ad_ids),
      2) fallback по fb_ad_name через fb_lookup.
    Служебные лиды исключаются (см. _is_service_lead): «Автосделка», «Рассылка Waba».
    Дедуп по lead_id (один лид считаем один раз).

    Args:
        leads        — RAW-лиды из get_leads/get_leads_window (ключ 'custom_fields',
                       'tags', 'status_id', 'id').
        fb_lookup    — {ad_name_lower: ad_id} из _build_fb_lookup.
        known_ad_ids — множество известных ad_id (str) для прямого матча по fb_ad_id.

    Returns:
        {ad_id: {"meetings_scheduled": int, "meetings_held": int}} — только ad_id,
        у которых есть хотя бы один лид на встрече. Пустой dict если встреч нет.
    """
    fb_lookup = fb_lookup or {}
    known_ad_ids = known_ad_ids or set()
    result: dict[str, dict] = {}
    seen_lead_ids: set = set()  # дедуп по lead_id — один лид считаем один раз

    for lead in leads:
        lead_id = lead.get("id")
        if lead_id is not None:
            if lead_id in seen_lead_ids:
                continue
            seen_lead_ids.add(lead_id)

        status_id = lead.get("status_id")
        if status_id is None:
            continue

        is_scheduled = status_id in MEETING_SCHEDULED_STATUS_IDS
        is_held = status_id in MEETING_HELD_STATUS_IDS
        if not is_scheduled and not is_held:
            continue

        # Служебные лиды (Автосделка/Рассылка Waba) не считаем — защита от завышения
        if _is_service_lead(lead):
            continue

        fb = _extract_fb_fields(lead)

        # Матчинг: сначала по точному fb_ad_id, затем fallback по имени (как match_leads_to_ads)
        ad_id = None
        lead_fb_ad_id = fb.get("ad_id")
        if lead_fb_ad_id and known_ad_ids and lead_fb_ad_id in known_ad_ids:
            ad_id = lead_fb_ad_id
        if not ad_id and fb.get("ad_name") and fb_lookup:
            ad_id = fb_lookup.get(fb["ad_name"].strip().lower())

        if not ad_id:
            continue

        if ad_id not in result:
            result[ad_id] = {"meetings_scheduled": 0, "meetings_held": 0}

        if is_scheduled:
            result[ad_id]["meetings_scheduled"] += 1
        if is_held:
            result[ad_id]["meetings_held"] += 1

    return result


def calc_ad_metrics(matched: dict, ad_spends: dict, meetings: dict | None = None) -> dict:
    """Считает метрики для каждого ad_id.
    ad_spends: {ad_id: spend} — расход из Facebook.
    meetings: {ad_id: {"meetings_scheduled", "meetings_held"}} — опционально, из
        count_meetings_by_ad (ARCH-hold-meetings). None/отсутствие ad_id → 0.
    Возвращает: {ad_id: {total_leads, qual_leads, qual_pct, cpql, payments, revenue, romi,
        meetings_scheduled, meetings_held}}."""
    result = {}
    meetings = meetings or {}

    # Курс из настроенного источника (или фиксированный из config) — считаем один раз
    from services.exchange_rate import get_usd_to_lcy
    usd_to_lcy = get_usd_to_lcy()

    for ad_id, data in matched.items():
        spend = ad_spends.get(ad_id, 0)
        total = data["total"]
        quals = data["quals"]
        payments = data["payments"]
        revenue = data["revenue"]
        ad_meetings = meetings.get(ad_id) or {}

        result[ad_id] = {
            "total_leads": total,
            "qual_leads": quals,
            "qual_pct": round(quals / total * 100, 1) if total > 0 else 0,
            "cpql": round(spend / quals, 2) if quals > 0 else None,
            "payments": payments,
            "revenue": revenue,
            # ROMI: расход конвертируем в ед. (выручка в LCY, расход в USD)
            "romi": round(revenue / (spend * usd_to_lcy) * 100, 1) if spend > 0 else None,
            "updated_at": datetime.now().isoformat(),
            "meetings_scheduled": ad_meetings.get("meetings_scheduled", 0),
            "meetings_held": ad_meetings.get("meetings_held", 0),
        }

    return result


# --- Полный цикл синхронизации ---

def get_qualified_leads_with_fb_id(days: int = 30) -> list[dict]:
    """Возвращает квалифицированные лиды из FB Lead Ads с их Facebook Lead ID.

    Используется для отправки MQL-событий в Facebook CAPI минуя Zapier.
    Возвращает список dict с ключами: id, fb_lead_id, created_at.
    Лиды без fb_lead_id пропускаются (значит пришли не из FB Lead Ads).
    """
    all_leads = get_leads(days=days)
    result = []
    for lead in all_leads:
        if not _is_qualified(lead):
            continue
        fb_lead_id = _extract_fb_lead_id(lead)
        if not fb_lead_id:
            continue
        result.append({
            "id": lead["id"],
            "fb_lead_id": fb_lead_id,
            "created_at": lead.get("created_at"),
        })
    return result


def sync_amo_data(ads: list[dict], days: int = 30) -> dict:
    """Полный цикл: выгрузка из AMO → матчинг по FB именам → расчёт метрик.
    ads: список объявлений из get_ads_with_metrics() (с именами и spend).
    Возвращает словарь {ad_id: metrics}. Сохранение — ответственность вызывающего."""
    ad_spends = {(ad.get("id") or ad.get("ad_id")): ad.get("spend", 0) for ad in ads}
    fb_lookup = _build_fb_lookup(ads)
    # C2: known_ad_ids для приоритетного матча по fb_ad_id (устраняет коллизию имён),
    # как в attach_amo_outcomes. Раньше /api/amo/sync матчил только по имени —
    # тёзки-объявления «крали» статистику друг у друга.
    known_ad_ids = {str(k) for k in ad_spends.keys() if k}
    leads = get_leads(days)
    matched = match_leads_to_ads(leads, fb_lookup=fb_lookup, known_ad_ids=known_ad_ids)
    # Встречи (ARCH-hold-meetings): тот же batched-проход по уже выгруженным лидам,
    # без дополнительных per-ad AMO-запросов — экономно.
    meetings = count_meetings_by_ad(leads, fb_lookup=fb_lookup, known_ad_ids=known_ad_ids)
    metrics = calc_ad_metrics(matched, ad_spends, meetings=meetings)

    return metrics
