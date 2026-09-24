"""
Модуль авто-запуска рекламы — бот сам решает, какие незапущенные карточки
Trello запустить и в какие города, чтобы закрыть пробелы покрытия.

ПРЕДОХРАНИТЕЛИ (по умолчанию всё выключено):
- autopilot.launch_enabled = false  (дополнительный флаг, независим от enabled)
- autopilot.enabled = false          (основной флаг автопилота)
- autopilot.kill_switch = true       (аварийное отключение)
- max_launches_per_day = 1           (не более 1 запуска в день)

ВАЖНО про бюджет:
launch_single → sealed Approval Gateway → provider adapter добавляет объявления
в СУЩЕСТВУЮЩИЙ адсет только после durable authorization.
Бюджет адсета задан в Facebook и НЕ меняется при запуске нового объявления.
Новые объявления конкурируют внутри того же бюджета адсета.
Поэтому «новый запуск» = новое объявление в уже работающем адсете.

Режимы:
- dry_run (ДЕФОЛТ): рекомендации + Telegram. Ничего не запускается.
- active: реальный запуск ≤ max_launches карточек. Только при launch_enabled=True.
"""

import fcntl
import html
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.formatting import fmt_money, truncate_at_word_boundary
from services.product_tags import PRODUCTS, classify_product

logger = logging.getLogger(__name__)

# Локальное время (UTC+5 по умолчанию, настраивается)
_TZ_LOCAL = timezone(timedelta(hours=5))

# State-файл: отслеживает запущенные сегодня карточки и историю запусков
_AUTO_LAUNCH_STATE_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "auto_launch_state.json"
)
_AUTO_LAUNCH_RUN_LOCK_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "locks" / "auto-launch-active.lock"
)
_ACTIVE_RUN_LEASE_TTL = timedelta(hours=2)

# Метка «мёртвой» темы — вето на запуск (из decision_policy.py)
_VETO_KEYWORD = "бонус"

# Маппинг меток Trello → campaign_type (как в существующем запуске)
_LABEL_TO_CAMPAIGN_TYPE = {
    "PRODA": "leadgen",
    "PRODB": "leadgen_prodb",
}

# Тип кампании по умолчанию (если метки нет — PRODA)
_DEFAULT_CAMPAIGN_TYPE = "leadgen"

# Ретеншн карты «id запуска → ad_ids» для кнопок Стоп. launched_ever — ВЕЧНЫЙ
# ledger «уже запускали» (его чистить нельзя — иначе карточка перезапустится),
# поэтому маппинг для кнопок держим в отдельном ключе stop_map со своим ретеншном.
STOP_MAP_RETENTION_DAYS = 30
MAX_STOP_ENTRIES = 200

# Лок для мутаций stop_map со стороны поллера (get/mark). Однопроцессный сервис;
# гонка с сохранением in-memory state в run_auto_launch задокументирована в
# _record_launch_stop (маловероятна: запуски ≤3/сут, окно клика ~секунды).
_STATE_LOCK = threading.Lock()

# «Все города» стандартного (оффлайн) запуска = ключи карты маршрутизации
# город→кабинет (services/launch_routing.py). CityF ВЕРНУЛАСЬ в этот список:
# город переехал в кабинет cabinet_b, и конвейер теперь маршрутизирует каждый
# город в его кабинет, поэтому карточка «Все города» снова покрывает все города карты.
# Функция (а не константа), потому что карту можно переопределить через
# settings.json без деплоя — список обязан читаться в момент запуска.
def _standard_cities() -> list[str]:
    from services.launch_routing import all_cities
    return all_cities()


# Метка Trello «Все города» = явное «текущее поведение»: карточка льётся во
# все города карты, даже если рядом стоят городские метки.
_ALL_CITIES_LABEL = "все города"


def _apply_skipped_targets(rec: dict, plan) -> None:
    """Города, пропущенные гейтом (уже запущены / нет слотов), убираются из контракта карточки.

    Решение владельца: город, где объявления уже есть от прошлого прогона,
    считается запущенным, пересоздавать не нужно. Остальные города запускаются.
    """
    skipped = tuple(getattr(plan, "skipped_targets", ()) or ())
    if not skipped:
        return
    kept = [str(target.city) for target in getattr(plan, "targets", ())]
    rec["cities"] = kept
    labels = {"ALREADY_EXISTS": "уже запущен", "DUPLICATE_LIVE": "уже запущен", "CAPACITY_BLOCKED": "нет слотов"}
    rec["skipped_cities"] = dict(
        rec.get("skipped_cities") or {},
        **{city: f"{labels.get(code, code)}: {reason}" for city, code, reason in skipped},
    )
    logger.warning(
        "auto_launch: карточка «%s» — города пропущены гейтом: %s; запускаю: %s",
        rec.get("card_name", rec.get("card_id")),
        ", ".join(f"{city} ({labels.get(code, code)})" for city, code, _r in skipped),
        ", ".join(kept),
    )


_CITY_SUFFIX_RE = re.compile(r"^(?P<city>.+?)(?:\s+\d+)?$")


def _city_from_card_name(name: object) -> list[str] | None:
    """Город из последнего сегмента названия карточки: «… / CityB 2» → ["CityB"].

    Решение владельца: без метки город в названии равен метке. Только точное имя города
    карты (с необязательным номером через пробел); иначе None — «все города» как раньше.
    """
    from services.launch_routing import known_cities

    text = str(name or "").strip()
    if " / " not in text:
        return None
    last = text.rsplit(" / ", 1)[1].strip()
    match = _CITY_SUFFIX_RE.match(last)
    if not match:
        return None
    candidate = match.group("city").strip().casefold()
    for city in known_cities():
        if city.casefold() == candidate:
            return _cities_from_labels([city])
    return None


def _cities_from_labels(labels: object) -> list[str] | None:
    """Целевые города карточки по её городским меткам Trello.

    Метка, совпадающая (без учёта регистра и пробелов) с городом карты
    маршрутизации, сужает запуск до этого города; несколько городских меток —
    до их множества в порядке карты. Нет городских меток или есть метка
    «Все города» → None (все города, как раньше). Городские метки есть, но ни
    один из городов сейчас не маршрутизирован (выключен через settings) →
    пустой список: запускать некуда, и это отказ, а не «все города».

    Исправленный баг: карточка «Карточка / CityA 3» с меткой
    «CityA» ушла в адсет CityF — метки до выбора городов не доходили.
    """
    if not isinstance(labels, list):
        return None
    from services.launch_routing import known_cities

    routed = _standard_cities()
    known = {city.casefold(): city for city in known_cities()}
    known.update({city.casefold(): city for city in routed})
    picked: set[str] = set()
    for label in labels:
        key = str(label or "").strip().casefold()
        if not key:
            continue
        if key == _ALL_CITIES_LABEL:
            return None
        city = known.get(key)
        if city is not None:
            picked.add(city)
    if not picked:
        return None
    return [city for city in routed if city in picked]


_TOKEN_RE = re.compile(
    r"(?i)(access_token(?:=|%3D)|authorization:\s*bearer\s+)[^&\s]+"
)


# ---------------------------------------------------------------------------
# State-файл авто-запуска
# ---------------------------------------------------------------------------

def _load_auto_launch_state() -> dict:
    """Загружает состояние авто-запуска из файла.

    launched_ever хранит либо старый формат (card_id: iso_datetime — только
    дата запуска), либо новый (card_id: {"at": iso_datetime, "name": str,
    "ad_ids": [str, ...]}) — имя карточки нужно для отчёта об остатке очереди,
    ad_ids (плоский список, необязательное поле) — для Контроля запуска
    (services/launch_verify.py). Обратная совместимость: старые записи (просто
    строка или dict без ad_ids) читаются наравне с новыми, см. _launched_at
    /_launched_name ниже — оба формата не ломают чтение.
    """
    default: dict = {
        "schema_version": 2,
        "launched_today": [],       # [card_id, ...] — запущены сегодня (сбрасывается каждый день)
        "proposed_today": [],       # [card_id, ...] — pending предложения за сегодня
        "launched_ever": {},        # {card_id: iso_datetime | {"at": iso, "name": str}}
        "launch_attempts": {},      # durable-журнал попыток по card_id+campaign_type
        "last_launch_date": None,   # строка "YYYY-MM-DD" последнего авто-запуска
        "last_proposal_date": None, # строка "YYYY-MM-DD" последнего предложения
    }
    if not _AUTO_LAUNCH_STATE_FILE.exists():
        return default
    try:
        data = json.loads(_AUTO_LAUNCH_STATE_FILE.read_text(encoding="utf-8"))
        default.update(data)
        default.setdefault("launch_attempts", {})
        default["schema_version"] = 2
        # Legacy v1 не имел достаточного city_plan для безопасной сверки.
        # Поэтому старые строки и dict сохраняем как complete: повторять их
        # вслепую опаснее, чем оставить на ручной аудит. Retryable-семантика
        # гарантируется для всех новых schema v2 attempts.
        for card_id, entry in list(default["launched_ever"].items()):
            if isinstance(entry, str):
                continue
            if not isinstance(entry, dict):
                del default["launched_ever"][card_id]
                continue
            if "complete" not in entry:
                entry["complete"] = True
                entry["completed_at"] = entry.get("at")
                if not entry.get("ad_ids"):
                    entry["legacy_unverified"] = True
        return default
    except Exception as exc:
        logger.warning("Не удалось загрузить auto_launch_state.json: %s", exc)
        return default


def _save_auto_launch_state(state: dict) -> None:
    """Атомарно сохраняет состояние авто-запуска (tmp + rename)."""
    _AUTO_LAUNCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _AUTO_LAUNCH_STATE_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(_AUTO_LAUNCH_STATE_FILE)
    except Exception as exc:
        logger.error("Не удалось сохранить auto_launch_state.json: %s", exc)
        raise


def _safe_error(error: object) -> str:
    """Удаляет токены из ошибок до логов, state и Telegram."""
    return _TOKEN_RE.sub(r"\1<redacted>", str(error))


def _parse_state_time(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_TZ_LOCAL)
    return parsed


def _acquire_active_run_lease() -> tuple[str, object] | None:
    """Неблокирующе захватывает межпроцессный active-run lock и lease.

    ``flock`` освобождается ядром при падении процесса. State-lease ограничен
    двумя часами, поэтому повреждённый/stale marker не создаёт вечный deadlock.
    """
    _AUTO_LAUNCH_RUN_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_file = _AUTO_LAUNCH_RUN_LOCK_FILE.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None

    run_id = uuid.uuid4().hex
    now = datetime.now(_TZ_LOCAL)
    try:
        with _STATE_LOCK:
            state = _load_auto_launch_state()
            current = state.get("active_run_lease")
            expires_at = _parse_state_time(
                current.get("expires_at") if isinstance(current, dict) else None
            )
            if expires_at and expires_at > now:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
                return None
            state["active_run_lease"] = {
                "run_id": run_id,
                "pid": os.getpid(),
                "acquired_at": now.isoformat(),
                "expires_at": (now + _ACTIVE_RUN_LEASE_TTL).isoformat(),
            }
            _save_auto_launch_state(state)
    except Exception:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
        raise
    return run_id, lock_file


def _release_active_run_lease(handle: tuple[str, object]) -> None:
    """Снимает только собственный lease и всегда освобождает kernel lock."""
    run_id, lock_file = handle
    try:
        with _STATE_LOCK:
            state = _load_auto_launch_state()
            current = state.get("active_run_lease")
            if isinstance(current, dict) and current.get("run_id") == run_id:
                state.pop("active_run_lease", None)
                _save_auto_launch_state(state)
    except Exception as exc:
        logger.error("Не удалось снять state lease auto_launch: %s", _safe_error(exc))
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _rollover_daily_state(today: str) -> dict:
    """Возвращает дневной view без преждевременной записи success-даты."""

    with _STATE_LOCK:
        state = _load_auto_launch_state()
        if state.get("last_launch_date") != today:
            state = dict(state)
            state["launched_today"] = []
        return state


def _rollover_proposed_today(today: str) -> list[str]:
    """Карточки, у которых уже есть pending предложение за сегодня.

    Отдельная дата нужна потому, что ``last_launch_date`` пишется только после
    verified CREATE: в дни без исполнения она отстаёт, и общий счётчик
    сбрасывался бы на каждом чтении.
    """

    with _STATE_LOCK:
        state = _load_auto_launch_state()
        if state.get("last_proposal_date") != today:
            return []
        return [str(card_id) for card_id in state.get("proposed_today", [])]


def _record_proposed_slot(card_id: str, today: str) -> None:
    """Занимает дневной слот сразу после создания owner proposal."""

    with _STATE_LOCK:
        state = _load_auto_launch_state()
        if state.get("last_proposal_date") != today:
            state["proposed_today"] = []
        proposed_today = state.setdefault("proposed_today", [])
        if card_id not in proposed_today:
            proposed_today.append(card_id)
        state["last_proposal_date"] = today
        _save_auto_launch_state(state)


def _get_today_str() -> str:
    """Возвращает сегодняшнюю дату по локальному времени как строку 'YYYY-MM-DD'."""
    return datetime.now(_TZ_LOCAL).date().isoformat()


def _launched_at(entry) -> str:
    """Достаёт iso-дату запуска из записи launched_ever — старый формат
    (запись = строка iso_datetime) и новый (запись = {"at":..., "name":...})."""
    if isinstance(entry, dict):
        return str(entry.get("at", ""))
    return str(entry)


def _launched_name(entry) -> str:
    """Достаёт имя карточки из записи launched_ever. Старый формат (строка,
    без имени) → пустая строка — не ломаем чтение, просто нет имени."""
    if isinstance(entry, dict):
        return str(entry.get("name", ""))
    return ""


def _launch_idempotency_key(card_id: str, campaign_type: str) -> str:
    """Стабильный ключ одной попытки запуска карточки данного типа."""
    raw = f"{card_id}|{campaign_type}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _replacement_launch_attempt_key(
    launch_key: str,
    city: str,
    adset_id: str,
) -> str:
    """Стабильный ownership-token exact city CREATE внутри общей карточки."""
    raw = f"{launch_key}|{city}|{adset_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _replacement_manifest_sha256(
    *,
    card_id: str,
    card_name: str,
    city: str,
    account_kind: str,
    account_id: str,
    adset_id: str,
    expected_ad_names: list[str],
    launch_media_manifest: object,
) -> str:
    """Добавляет exact card ID к provider-bound manifest actual media bytes."""
    manifest_hash = getattr(launch_media_manifest, "manifest_sha256", None)
    files = getattr(launch_media_manifest, "files", None)
    expected_card_name_hash = hashlib.sha256(card_name.encode("utf-8")).hexdigest()
    if (
        not isinstance(manifest_hash, str)
        or len(manifest_hash) != 64
        or any(char not in "0123456789abcdef" for char in manifest_hash)
        or not isinstance(files, tuple)
        or not files
        or getattr(launch_media_manifest, "card_name_sha256", None)
        != expected_card_name_hash
        or getattr(launch_media_manifest, "city", None) != city
        or getattr(launch_media_manifest, "account_kind", None) != account_kind
        or str(getattr(launch_media_manifest, "account_id", "")).removeprefix("act_")
        != str(account_id).removeprefix("act_")
        or str(getattr(launch_media_manifest, "adset_id", "")) != str(adset_id)
        or tuple(getattr(launch_media_manifest, "expected_ad_names", ()))
        != tuple(expected_ad_names)
    ):
        raise RuntimeError("Launch media manifest не совпадает с exact city scope")
    manifest = {
        "card_id": card_id,
        "launch_media_manifest_sha256": manifest_hash,
    }
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _target_cities(rec: dict) -> list[str]:
    """Возвращает полный и детерминированный список целевых городов.

    Порядок: явный rec["cities"] → онлайн-контур → городские метки карточки
    (rec["labels"], см. _cities_from_labels) → все города карты. Метки есть,
    но их города выключены из карты — отказ CITY_LABEL_UNROUTED, а не «все
    города»: пустой список городов ниже по стеку читается как «все».
    """
    explicit = rec.get("cities")
    if explicit:
        return list(dict.fromkeys(str(city).strip() for city in explicit if str(city).strip()))
    if rec.get("campaign_type") in _ONLINE_CAMPAIGN_TYPES:
        return [_ONLINE_CITY]
    labeled = _cities_from_labels(rec.get("labels"))
    if labeled is not None:
        if not labeled:
            from services.launch_checker import LaunchCheckBlocked

            raise LaunchCheckBlocked(
                "CITY_LABEL_UNROUTED",
                (_CITY_LABEL_UNROUTED_REASON,),
                None,
            )
        return _drop_cities_with_paused_adset(rec, labeled)
    return _drop_cities_with_paused_adset(rec, _standard_cities())


_PAUSED_ADSET_STATUSES = frozenset({"PAUSED", "ADSET_PAUSED", "CAMPAIGN_PAUSED", "ARCHIVED", "DELETED"})


def _drop_cities_with_paused_adset(rec: dict, cities: list[str]) -> list[str]:
    """Правило владельца: выключенный адсет города не блокирует карточку, город пропускается.

    До этого один PAUSED адсет (например, выключенный L1-адсет одного города) давал INVENTORY_UNVERIFIED на всю
    карточку, и остальные города не запускались. Пропущенные города записываются в rec["skipped_cities"]
    для журнала. Живой статус недоступен или неизвестен → город остаётся (прежнее поведение, страж инвентаря
    скажет своё). Все адсеты выключены → отказ ALL_ADSETS_PAUSED.
    """
    if not cities:
        return cities
    try:
        statuses = _city_adset_statuses(rec, cities)
    except Exception as exc:  # noqa: BLE001 — недоступность FB не меняет прежнее поведение
        logger.warning("auto_launch: статусы адсетов не прочитаны (%s) — города не фильтрую", str(exc)[:120])
        return cities
    kept: list[str] = []
    skipped: dict[str, str] = {}
    for city in cities:
        status = statuses.get(city)
        if status in _PAUSED_ADSET_STATUSES:
            skipped[city] = f"адсет выключен ({status})"
        else:
            kept.append(city)
    if skipped:
        rec["skipped_cities"] = dict(rec.get("skipped_cities") or {}, **skipped)
        logger.warning(
            "auto_launch: карточка «%s» — пропущены города с выключенным адсетом: %s",
            rec.get("card_name", rec.get("card_id")),
            ", ".join(f"{city} ({reason})" for city, reason in skipped.items()),
        )
    if not kept:
        from services.launch_checker import LaunchCheckBlocked

        raise LaunchCheckBlocked(
            "ALL_ADSETS_PAUSED",
            ("Во всех целевых городах адсеты выключены — запускать некуда",),
            None,
        )
    return kept


def _city_adset_statuses(rec: dict, cities: list[str]) -> dict[str, str]:
    """{город: effective_status адсета} для типа карточки; города без адсета в карте не попадают."""
    from integrations import facebook
    from services.language import detect_language
    from services.launch_staging import _account_context

    campaign_type = str(rec.get("campaign_type") or "")
    with _account_context(campaign_type):
        adset_type = detect_language(str(rec.get("card_name") or ""), str(rec.get("card_desc") or ""))
        pairs = facebook._resolve_launch_adsets(campaign_type, adset_type, list(cities))  # noqa: SLF001
        statuses = facebook.adset_effective_statuses([adset_id for _, adset_id in pairs])
    return {city: statuses.get(adset_id, "UNKNOWN") for city, adset_id in pairs}


_CITY_LABEL_UNROUTED_REASON = (
    "Городские метки карточки не входят в карту маршрутизации запуска"
)


def _resolve_launch_account(campaign_type: str) -> tuple[str, str]:
    """Фиксирует кабинет попытки, чтобы crash-reconcile не сменил аккаунт."""
    from services.fb_token_provider import fb_account, get_fb_account_id

    account_kind = "online" if campaign_type in _ONLINE_CAMPAIGN_TYPES else "offline"
    context_name = "online" if account_kind == "online" else None
    with fb_account(context_name):
        account_id = str(get_fb_account_id()).removeprefix("act_")
    if not account_id:
        raise RuntimeError("Не удалось определить FB account_id запуска")
    return account_kind, account_id


def _prepare_launch_attempt(rec: dict, target_cities: list[str]) -> str:
    """Создаёт durable PREPARED-попытку, не помечая карточку запущенной."""
    card_id = str(rec["card_id"])
    campaign_type = str(rec["campaign_type"])
    key = _launch_idempotency_key(card_id, campaign_type)
    now = datetime.now(_TZ_LOCAL)
    now_iso = now.isoformat()
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempts = state.setdefault("launch_attempts", {})
        existing = attempts.get(key)
        if isinstance(existing, dict):
            return key
        account_kind, account_id = _resolve_launch_account(campaign_type)
        attempts[key] = {
            # Gateway принимает только canonical UUID4; ключ словаря
            # остаётся stable SHA для поиска card+campaign attempt.
            "idempotency_key": str(uuid.uuid4()),
            "card_id": card_id,
            "card_name": str(rec.get("card_name", "")),
            "campaign_type": campaign_type,
            "fb_account_kind": account_kind,
            "fb_account_id": account_id,
            "target_cities": list(target_cities),
            "pending_cities": list(target_cities),
            "city_plan": {},
            "phase": "PREPARED",
            "ads_by_city": {},
            "errors_by_city": {},
            "started_at": now_iso,
            "updated_at": now_iso,
            "finished_at": None,
        }
        state["schema_version"] = 2
        _save_auto_launch_state(state)
    return key


# Терминальные lifecycle-состояния предложения: ключ идемпотентности занят
# навсегда, повторное предложение той же карточки требует ротации ключа.
_TERMINAL_PROPOSAL_STATES = frozenset(
    {"BLOCKED_STALE", "EXPIRED", "FAILED_NO_EFFECT", "REJECTED", "CANCELLED"}
)

# Фазы попытки, в которых ротация ключа допустима. Безопасность даёт не фаза,
# а отсутствие CREATE-evidence (проверяется отдельно): BLOCKED без единого
# create_started_at — это смерть ДО провайдера (отклонённый live review,
# слепая сверка, immutable-mismatch планов), ключ и планы можно обновлять.
# LAUNCHING/RECONCILING исключены всегда — процесс жив.
_ROTATION_SAFE_PHASES = frozenset({"PREPARED", "FAILED_RETRYABLE", "BLOCKED"})

# Предложение «отработано» — владелец-слой больше ничего по нему не исполняет: терминальные состояния
# плюс успешные EXECUTED/VERIFIED/COMPLETE (частичный запуск: часть claim'ов CONFIRMED, часть
# FAILED_NO_EFFECT). Только после этого попытку можно сверять как «чего нет — того нет» и ротировать
# ключ на дозапуск недостающих городов.
_SETTLED_PROPOSAL_STATES = _TERMINAL_PROPOSAL_STATES | frozenset({"EXECUTED", "VERIFIED", "COMPLETE"})


def _proposal_settled(idempotency_key: str) -> bool:
    """True, если предложение по ключу отработано владелец-слоем; любая ошибка → False."""
    if not idempotency_key:
        return False
    try:
        from services.owner_action_repository import find_proposal_state_by_idempotency_key

        return find_proposal_state_by_idempotency_key(idempotency_key) in _SETTLED_PROPOSAL_STATES
    except Exception as exc:  # noqa: BLE001 — неизвестность = не отработано
        logger.debug("auto_launch: состояние предложения %s недоступно — %s", idempotency_key, exc)
        return False


def _attempt_has_unresolved_create(attempt: dict) -> bool:
    """Город с CREATE-маркером, но без ad_id — исход неизвестен, ротировать нельзя."""
    city_plan = attempt.get("city_plan")
    ads_by_city = attempt.get("ads_by_city") or {}
    return isinstance(city_plan, dict) and any(
        isinstance(plan, dict) and plan.get("create_started_at") and not ads_by_city.get(city)
        for city, plan in city_plan.items()
    )


def _attempt_has_create_evidence(attempt: dict) -> bool:
    """CREATE мог начаться: город с create_started_at или сохранённые ad_id.

    Сам по себе city_plan — НЕ доказательство CREATE: планы пишутся на
    staging'е live review, то есть и у предложений, отклонённых до первого
    вызова провайдера. Доказательство — отметка create_started_at города
    (ставится после durable-резервации, непосредственно перед провайдером;
    адаптер снимает её, если исполнитель упал до первого claim) либо
    записанные ad_id.
    """
    city_plan = attempt.get("city_plan")
    if isinstance(city_plan, dict) and any(
        isinstance(plan, dict) and plan.get("create_started_at")
        for plan in city_plan.values()
    ):
        return True
    ads_by_city = attempt.get("ads_by_city")
    return isinstance(ads_by_city, dict) and any(
        bool(ad_ids) for ad_ids in ads_by_city.values()
    )


RESUME_MAX_AGE_DAYS = 30  # решение владельца: старше — закрывать без дозапуска


def attempt_resumable(attempt: object, *, now: datetime | None = None) -> bool:
    """Попытка ждёт дозапуска недостающих городов.

    Есть города с объявлениями и города без них, попытка не завершена и моложе RESUME_MAX_AGE_DAYS.
    Такая карточка не получает галочку Trello, берётся автозапуском даже с галочкой и не занимает
    дневной лимит запусков.
    """
    if not isinstance(attempt, dict):
        return False
    if str(attempt.get("phase") or "").upper() == "SUCCEEDED":
        return False
    ads_by_city = attempt.get("ads_by_city") or {}
    done = {city for city, ids in ads_by_city.items() if ids}
    targets = [str(city) for city in attempt.get("target_cities") or []]
    missing = [city for city in targets if city not in done]
    if not done or not missing:
        return False
    started = _parse_fb_time(attempt.get("started_at"))
    moment = now or datetime.now(_TZ_LOCAL)
    if started is None:
        return False
    return (moment - started) <= timedelta(days=RESUME_MAX_AGE_DAYS)


def resumable_card_ids(state: dict | None = None, *, now: datetime | None = None) -> set[str]:
    """card_id карточек с попыткой, ждущей дозапуска."""
    source = state if state is not None else _load_auto_launch_state()
    attempts = source.get("launch_attempts", {}) if isinstance(source, dict) else {}
    return {
        str(attempt.get("card_id") or "")
        for attempt in (attempts.values() if isinstance(attempts, dict) else ())
        if attempt_resumable(attempt, now=now)
    }


def _partial_rotation_allowed(attempt: dict) -> bool:
    """Частичная попытка (часть городов с ad_id) может уйти на дозапуск недостающих городов.

    Условия: фаза PARTIAL/FAILED_RETRYABLE, хотя бы один город с объявлениями, хотя бы один город
    без них, и ни одного города с неизвестным исходом CREATE. Состояние предложения проверяется
    отдельно (должно быть отработано).
    """
    if str(attempt.get("phase") or "") not in {"PARTIAL", "FAILED_RETRYABLE"}:
        return False
    ads_by_city = attempt.get("ads_by_city") or {}
    done = {city for city, ids in ads_by_city.items() if ids}
    missing = [city for city in attempt.get("target_cities", []) if city not in done]
    return bool(done) and bool(missing) and not _attempt_has_unresolved_create(attempt)


def _rotate_poisoned_attempt_key(key: str) -> bool:
    """Ротирует UUID durable-попытки, чьё предложение умерло терминально.

    Терминальное предложение занимает idempotency-ключ навсегда, а каждый
    новый план несёт свежие claim_id/valid_until/capacity — тот же ключ с
    другим plan_sha256 падает в IDEMPOTENCY_PAYLOAD_CONFLICT, и карточка
    тихо выбывает из автоматизации (август: 174 BLOCKED_STALE именно так
    закрыли конвейер). Ротация разрешена только когда CREATE доказуемо не
    начинался: фаза PREPARED/FAILED_RETRYABLE и ни следа city_plan/ad_id.
    """
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.get("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict):
            return False
        current_key = str(attempt.get("idempotency_key") or "")
        if not current_key:
            return False
        if _partial_rotation_allowed(attempt):
            partial = True
        elif (
            str(attempt.get("phase") or "") not in _ROTATION_SAFE_PHASES
            or _attempt_has_create_evidence(attempt)
        ):
            return False
        else:
            partial = False

    # Чтение БД — вне state-lock и best-effort: дренаж не имеет права
    # останавливать создание предложений, любая ошибка = «не ротируем».
    try:
        from services.owner_action_repository import (
            find_proposal_state_by_idempotency_key,
        )

        proposal_state = find_proposal_state_by_idempotency_key(current_key)
    except Exception as exc:  # noqa: BLE001 — недоступная БД не блокирует конвейер
        logger.debug("auto_launch: проверка отравления ключа %s недоступна — %s", key, exc)
        return False
    allowed_states = _SETTLED_PROPOSAL_STATES if partial else _TERMINAL_PROPOSAL_STATES
    if proposal_state not in allowed_states:
        return False

    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.get("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict) or str(attempt.get("idempotency_key") or "") != current_key:
            return False
        if partial:
            if not _partial_rotation_allowed(attempt):
                return False
        elif (
            str(attempt.get("phase") or "") not in _ROTATION_SAFE_PHASES
            or _attempt_has_create_evidence(attempt)
        ):
            return False
        attempt["idempotency_key"] = str(uuid.uuid4())
        attempt["rotated_from"] = current_key
        attempt["rotated_at"] = datetime.now(_TZ_LOCAL).isoformat()
        attempt["phase"] = "PREPARED"
        ads_by_city = attempt.get("ads_by_city") or {}
        done = {city for city, ids in ads_by_city.items() if ids}
        # Планы и ошибки мёртвого предложения не должны прилипнуть к новому:
        # staging нового предложения даёт другой manifest sha, и старый план
        # уронил бы его «Immutable city plan изменился». Города с объявлениями
        # (частичный запуск) сохраняют план и ad_id — дозапуск только недостающих.
        attempt["city_plan"] = {
            city: plan for city, plan in (attempt.get("city_plan") or {}).items() if city in done
        }
        attempt["errors_by_city"] = {}
        attempt["pending_cities"] = [
            city for city in attempt.get("target_cities", []) if city not in done
        ]
        attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
        _save_auto_launch_state(state)
    logger.info(
        "auto_launch: ключ попытки %s ротирован — предложение терминально (%s)",
        key,
        proposal_state,
    )
    return True


def _attempt_in_flight(key: str, attempt: dict) -> bool | None:
    """Попытка принадлежит живому предложению: ждёт владельца или исполняется.

    Фаза LAUNCHING сама по себе не значит «процесс упал»: гейтвей исполняет
    карточку по одному claim за тик исполнителя (30 мин, бюджет задания 5 мин),
    и цепочка из 6–12 объявлений часами живёт в QUEUED/WAITING_RETRY/REVIEWING,
    а до одобрения попытка стоит в LAUNCHING со стейджинга. Сверка такой
    попытки по живому инвентарю (например, карточка с 2 объявлениями из 6)
    давала BLOCKED/FAILED_RETRYABLE, следующий claim падал в
    ``_mark_city_create_started`` → RECONCILE_REQUIRED, хвост цепочки не
    исполнялся.

    None — БД контура одобрений не прочитана. Вызывающий обязан трактовать
    это fail-closed: не сверять.
    """
    idempotency_key = str(attempt.get("idempotency_key") or "")
    if not idempotency_key:
        return False
    try:
        from services.owner_action_repository import (
            proposal_in_flight_by_idempotency_key,
        )

        return bool(proposal_in_flight_by_idempotency_key(idempotency_key))
    except Exception as exc:  # noqa: BLE001 — неизвестность = не сверять
        logger.warning(
            "auto_launch: не удалось проверить живое предложение попытки %s — %s",
            key,
            _safe_error(exc),
        )
        return None


def _gateway_attempt(key: str, idempotency_key: str) -> dict:
    """Возвращает exact durable attempt для server-owned gateway запуска."""

    try:
        parsed = uuid.UUID(idempotency_key)
    except (TypeError, ValueError, AttributeError) as exc:
        raise RuntimeError("Auto-launch attempt не содержит canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != idempotency_key:
        raise RuntimeError("Auto-launch attempt не содержит canonical UUID4")
    attempt = _load_auto_launch_state().get("launch_attempts", {}).get(key)
    if not isinstance(attempt, dict) or attempt.get("idempotency_key") != idempotency_key:
        raise RuntimeError("Auto-launch durable attempt изменился")
    return attempt


def _find_gateway_attempt(idempotency_key: str) -> tuple[str, dict]:
    attempts = _load_auto_launch_state().get("launch_attempts", {})
    matches = [
        (str(key), value)
        for key, value in attempts.items()
        if isinstance(value, dict) and value.get("idempotency_key") == idempotency_key
    ]
    if len(matches) != 1:
        raise RuntimeError("Auto-launch gateway attempt не найден однозначно")
    return matches[0]


def prepare_staged_launch_for_gateway(prepared, idempotency_key: str):
    """Bind/release/claim replacement до fresh capacity и gateway review.

    Все операции идут под теми же sorted adset locks. Durable phase
    делает bind, slot release и claim идемпотентными при replay.
    """

    from contextlib import ExitStack
    from decimal import Decimal, InvalidOperation

    from integrations import facebook
    from services.adset_pause_guard import adset_mutation_lock
    from services.launch_staging import refresh_staged_destinations

    key, attempt = _find_gateway_attempt(idempotency_key)
    if (
        str(attempt.get("card_id") or "") != prepared.trello.card_id
        or str(attempt.get("campaign_type") or "") != prepared.campaign_type
    ):
        raise RuntimeError("Auto-launch staged scope не совпадает с attempt")
    rec = {
        "card_id": prepared.trello.card_id,
        "card_name": prepared.card_name,
        "campaign_type": prepared.campaign_type,
    }
    refreshed = []
    with ExitStack() as locks:
        for destination in sorted(
            prepared.destinations,
            key=lambda item: int(item.adset_id),
        ):
            if not destination.adset_id.isdigit() or not destination.account_id.isdigit():
                raise RuntimeError("Auto-launch требует numeric Meta IDs")
            locks.enter_context(adset_mutation_lock(destination.adset_id))
        for destination in prepared.destinations:
            names = [creative.ad_name for creative in destination.creatives]
            binding_payload = {
                "card_id": prepared.trello.card_id,
                "city": destination.city,
                "adset_id": destination.adset_id,
                "media_manifest_sha256": prepared.media_manifest_sha256,
                "expected_ad_names": names,
            }
            binding_sha256 = hashlib.sha256(
                json.dumps(
                    binding_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            _prepare_city_launch(
                key,
                destination.city,
                destination.adset_id,
                names,
                len(names),
                media_manifest_sha256=binding_sha256,
                account_id=destination.account_id,
            )
            workflow_id = _prepare_replacement_workflow_for_city(
                key,
                rec,
                attempt,
                destination.city,
                destination.adset_id,
                names,
                binding_sha256,
            )
            capacity = facebook.get_adset_capacity(destination.adset_id)
            try:
                available = capacity["available"]
                daily_budget = Decimal(str(capacity["daily_budget"]))
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise RuntimeError("Fresh adset capacity неполна") from exc
            if type(available) is not int or available < 0 or not daily_budget.is_finite():
                raise RuntimeError("Fresh adset capacity некорректна")
            refreshed.append(
                replace(
                    destination,
                    capacity_available=available,
                    current_daily_budget=daily_budget,
                    hard_reserve_slots=facebook._get_launch_hard_reserve_slots(),
                    replacement_workflow_id=workflow_id,
                )
            )
        return refresh_staged_destinations(prepared, tuple(refreshed))


def mark_gateway_create_started(manifest) -> None:
    """Фиксирует CREATE_STARTED после durable-резервации, до provider.

    Порядок в адаптере: резервация → маркер → исполнитель. Маркер до
    резервации был ложной уликой: падение резервации (capacity, дубль)
    оставляло create_started_at, сверка блокировала попытку «после
    CREATE_STARTED», ротация ключа запрещалась — выхода не было. Падение
    исполнителя до первого claim снимает маркер —
    ``unmark_gateway_create_started``.
    """

    key, _attempt = _find_gateway_attempt(manifest.idempotency_key)
    state = _load_auto_launch_state()
    current = state["launch_attempts"][key]
    for destination in manifest.destinations:
        plan = current.get("city_plan", {}).get(destination.city)
        if not isinstance(plan, dict):
            raise RuntimeError("Auto-launch city plan не сохранён до CREATE")
        _mark_city_create_started(
            key,
            destination.city,
            str(plan.get("media_manifest_sha256") or ""),
        )


def record_gateway_launch_no_effect(manifest, reason: str) -> None:
    """Город(а) манифеста: исполнитель упал ДО провайдера, объявлений нет.

    Ошибка города пишется в errors_by_city, город остаётся pending, маркер CREATE снимается там, где
    ad_id нет — сверка увидит «без маркера и без объявлений» и отдаст FAILED_RETRYABLE вместо BLOCKED,
    а дренаж сможет ротировать ключ и предложить город заново.
    """
    key, _attempt = _find_gateway_attempt(manifest.idempotency_key)
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.get("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict):
            raise KeyError(f"Попытка запуска {key} не найдена")
        city_plan = attempt.setdefault("city_plan", {})
        ads_by_city = attempt.get("ads_by_city", {})
        errors = attempt.setdefault("errors_by_city", {})
        for destination in manifest.destinations:
            if ads_by_city.get(destination.city):
                continue
            errors[destination.city] = _safe_error(f"no_effect: {reason}")
            plan = city_plan.get(destination.city)
            if isinstance(plan, dict):
                plan.pop("create_started_at", None)
        attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
        _save_auto_launch_state(state)


def unmark_gateway_create_started(manifest) -> None:
    """Снимает CREATE-маркер городов манифеста: провайдер объявлений не создавал.

    Зовётся адаптером только когда репозиторий подтвердил отсутствие claim по
    авторизации. Город с уже записанными ad_id не трогаем — там маркер
    честный. Сверка после этого видит нулевые совпадения без маркера и
    отдаёт FAILED_RETRYABLE, а дренаж может ротировать ключ.
    """

    key, _attempt = _find_gateway_attempt(manifest.idempotency_key)
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.get("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict):
            raise KeyError(f"Попытка запуска {key} не найдена")
        city_plan = attempt.get("city_plan", {})
        ads_by_city = attempt.get("ads_by_city", {})
        changed = False
        for destination in manifest.destinations:
            plan = city_plan.get(destination.city)
            if not isinstance(plan, dict) or ads_by_city.get(destination.city):
                continue
            if plan.pop("create_started_at", None) is not None:
                changed = True
        if not changed:
            return
        attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
        _save_auto_launch_state(state)
    logger.info(
        "auto_launch: CREATE-маркер попытки %s снят — исполнитель упал до первого claim",
        key,
    )


def record_gateway_launch_result(manifest, created_ids: tuple[str, ...]) -> None:
    """Связывает exact provider IDs с replacement workflow без callbacks."""

    key, _attempt = _find_gateway_attempt(manifest.idempotency_key)
    offset = 0
    for destination in manifest.destinations:
        count = len(destination.creatives)
        city_ids = list(created_ids[offset : offset + count])
        if len(city_ids) != count:
            raise RuntimeError("Gateway вернул partial city IDs")
        # Один durable commit сохраняет и replacement result, и auto-launch
        # ledger. Внешний caller увидит ads_by_city и не повторит запись.
        _record_city_success(key, destination.city, city_ids)
        offset += count


def _ensure_attempt_account_for_create(key: str, campaign_type: str) -> dict:
    """Проверяет кабинет перед retry/CREATE и backfill-ит безопасный legacy.

    Legacy attempt без account evidence можно backfill-ить только до первого
    признака CREATE: без сохранённых ad_id и без city_plan. Иначе кабинет
    созданных объявлений неоднозначен, поэтому attempt атомарно BLOCKED.
    """
    try:
        current_kind, current_id = _resolve_launch_account(campaign_type)
    except Exception as exc:
        safe_error = _safe_error(exc)
        with _STATE_LOCK:
            state = _load_auto_launch_state()
            attempt = state.setdefault("launch_attempts", {}).get(key)
            if isinstance(attempt, dict):
                attempt["phase"] = "BLOCKED"
                attempt.setdefault("errors_by_city", {})["__account__"] = (
                    f"Не удалось определить текущий FB кабинет: {safe_error}"
                )
                attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
                _save_auto_launch_state(state)
        raise RuntimeError("Запуск заблокирован: FB кабинет не определён") from None

    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.setdefault("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict):
            raise KeyError(f"Попытка запуска {key} не найдена")
        phase = str(attempt.get("phase", ""))
        saved_kind = str(attempt.get("fb_account_kind", ""))
        saved_id = str(attempt.get("fb_account_id", "")).removeprefix("act_")

        if phase == "BLOCKED":
            raise RuntimeError("Запуск заблокирован ранее и требует ручной проверки")

        if not saved_kind or not saved_id:
            ads_by_city = attempt.get("ads_by_city", {})
            city_plan = attempt.get("city_plan", {})
            has_created_ads = not isinstance(ads_by_city, dict) or any(
                bool(ad_ids) for ad_ids in ads_by_city.values()
            )
            has_city_plan = not isinstance(city_plan, dict) or bool(city_plan)
            if phase in {"LAUNCHING", "RECONCILING"} or has_created_ads or has_city_plan:
                legacy_context = (
                    "legacy LAUNCHING"
                    if phase in {"LAUNCHING", "RECONCILING"}
                    else "legacy attempt с CREATE evidence"
                )
                attempt["phase"] = "BLOCKED"
                attempt.setdefault("errors_by_city", {})["__account__"] = (
                    f"{legacy_context} без сохранённого FB кабинета"
                )
                attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
                _save_auto_launch_state(state)
                raise RuntimeError(
                    f"Запуск заблокирован: {legacy_context} без FB кабинета"
                )
            if phase not in {"PREPARED", "PARTIAL", "FAILED_RETRYABLE"}:
                raise RuntimeError("Запуск заблокирован: account backfill недопустим")
            attempt["fb_account_kind"] = current_kind
            attempt["fb_account_id"] = current_id
            attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
            _save_auto_launch_state(state)
            return dict(attempt)

        if saved_kind != current_kind or saved_id != current_id:
            attempt["phase"] = "BLOCKED"
            attempt.setdefault("errors_by_city", {})["__account__"] = (
                "Текущий FB кабинет не совпадает с кабинетом launch_attempt"
            )
            attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
            _save_auto_launch_state(state)
            raise RuntimeError("Запуск заблокирован: FB account mismatch")
        return dict(attempt)


def _start_retry_cycle(key: str) -> None:
    """Открывает новое безопасное окно для retry только после reconciliation."""
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.setdefault("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict):
            raise KeyError(f"Попытка запуска {key} не найдена")
        if attempt.get("phase") not in {"PARTIAL", "FAILED_RETRYABLE", "PREPARED"}:
            return
        now_iso = datetime.now(_TZ_LOCAL).isoformat()
        attempt.setdefault("first_started_at", attempt.get("started_at", now_iso))
        attempt["started_at"] = now_iso
        attempt["phase"] = "PREPARED"
        attempt["updated_at"] = now_iso
        _save_auto_launch_state(state)


def _prepare_city_launch(
    key: str,
    city: str,
    target_adset_id: str,
    expected_ad_names: list[str],
    expected_ads_count: int,
    media_manifest_sha256: str | None = None,
    account_id: str | None = None,
) -> None:
    """Сохраняет точный план города непосредственно перед первым CREATE.

    ``account_id`` — кабинет назначения ЭТОГО города. Карта роутинга разносит
    города одной карточки по двум кабинетам (L2 → cabinet_b, L1 → cabinet_a,
    CityF → cabinet_b), а кабинет уровня попытки — всегда дефолтный для
    вида кампании. Без account_id в city_plan crash-reconciliation читает
    один кабинет и структурно слепнет ко второму: создание подтверждено в FB,
    а попытка навсегда уходит в BLOCKED.

    Окно сверки строится от текущего времени, не от ``attempt.started_at``:
    попытка живёт неделями (ключ card+campaign), started_at ставится один раз
    при первой подготовке и в боевом пути не обновляется. План ротированной
    попытки получал окно из прошлого месяца, и любые объявления оказывались
    «вне окна» → BLOCKED. Точная привязка окна к CREATE — в
    ``_mark_city_create_started``.
    """
    names = [str(name) for name in expected_ad_names]
    if expected_ads_count <= 0 or len(names) != expected_ads_count:
        raise ValueError("Некорректный план запуска: count не совпадает с именами")
    if len(set(names)) != len(names):
        raise ValueError("Некорректный план запуска: имена объявлений не уникальны")
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.setdefault("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict):
            raise KeyError(f"Попытка запуска {key} не найдена")
        if attempt.get("phase") == "BLOCKED":
            raise RuntimeError("Попытка запуска заблокирована reconciliation")
        now = datetime.now(_TZ_LOCAL)
        existing_plan = attempt.setdefault("city_plan", {}).get(city)
        if isinstance(existing_plan, dict):
            expected_manifest = existing_plan.get("media_manifest_sha256")
            existing_account = existing_plan.get("account_id")
            exact_match = (
                str(existing_plan.get("target_adset_id") or "")
                == str(target_adset_id)
                and existing_plan.get("expected_ad_names") == names
                and existing_plan.get("expected_ads_count") == expected_ads_count
                and expected_manifest == media_manifest_sha256
                # Кабинет — часть identity плана, когда известен с обеих
                # сторон; legacy-план без account_id backfill-ится ниже.
                and (
                    existing_account is None
                    or account_id is None
                    or str(existing_account) == str(account_id)
                )
            )
            if not exact_match:
                attempt["phase"] = "BLOCKED"
                attempt.setdefault("errors_by_city", {})[city] = (
                    "Immutable city plan изменился; требуется ручная проверка"
                )
                attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
                _save_auto_launch_state(state)
                raise RuntimeError("Immutable city plan mismatch")
            existing_until = _parse_fb_time(existing_plan.get("reconcile_until"))
            retry_until = now + timedelta(hours=2)
            if existing_until is None:
                attempt["phase"] = "BLOCKED"
                attempt.setdefault("errors_by_city", {})[city] = (
                    "Immutable city plan содержит invalid reconcile_until"
                )
                attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
                _save_auto_launch_state(state)
                raise RuntimeError("Immutable city plan window invalid")
            existing_plan["reconcile_until"] = max(existing_until, retry_until).isoformat()
            if existing_account is None and account_id is not None:
                existing_plan["account_id"] = str(account_id).removeprefix("act_")
            attempt["phase"] = "LAUNCHING"
            attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
            _save_auto_launch_state(state)
            return
        city_plan = {
            "target_adset_id": str(target_adset_id),
            "expected_ad_names": names,
            "expected_ads_count": expected_ads_count,
            "reconcile_from": (now - timedelta(minutes=5)).isoformat(),
            "reconcile_until": (now + timedelta(hours=2)).isoformat(),
        }
        if account_id is not None:
            city_plan["account_id"] = str(account_id).removeprefix("act_")
        if media_manifest_sha256 is not None:
            if (
                len(media_manifest_sha256) != 64
                or any(char not in "0123456789abcdef" for char in media_manifest_sha256)
            ):
                raise ValueError("Некорректный media manifest SHA-256")
            city_plan["media_manifest_sha256"] = media_manifest_sha256
        attempt["city_plan"][city] = city_plan
        attempt["phase"] = "LAUNCHING"
        attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
        _save_auto_launch_state(state)


def _save_city_replacement_binding(
    key: str,
    city: str,
    *,
    workflow_id: str,
    launch_attempt_key: str,
    media_manifest_sha256: str,
) -> None:
    """Сохраняет ссылку на durable workflow до возможного CREATE."""
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.setdefault("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict):
            raise KeyError(f"Попытка запуска {key} не найдена")
        plan = attempt.setdefault("city_plan", {}).get(city)
        if not isinstance(plan, dict):
            raise RuntimeError("City plan отсутствует до replacement binding")
        existing = plan.get("replacement_workflow_id")
        if existing not in {None, workflow_id}:
            raise RuntimeError("City plan уже связан с другим replacement workflow")
        plan.update(
            {
                "replacement_workflow_id": workflow_id,
                "replacement_launch_attempt_key": launch_attempt_key,
                "replacement_media_manifest_sha256": media_manifest_sha256,
            }
        )
        attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
        _save_auto_launch_state(state)


def _mark_city_create_started(
    key: str,
    city: str,
    media_manifest_sha256: str,
) -> None:
    """Durable marker непосредственно перед первым provider CREATE.

    Здесь же окно сверки привязывается к реальному моменту CREATE. План
    пишется на staging'е предложения, а владелец одобряет позже — обычно
    утренним пакетом на следующий день, то есть за пределами «staging + 2 ч».
    Первый маркер сдвигает reconcile_from на «маркер − 5 мин» (раньше
    объявления появиться не могли), любой маркер продлевает reconcile_until
    до «сейчас + 2 ч». Повторный маркер (retry после настоящего
    CREATE_STARTED) начало окна не двигает: объявления могли появиться и в
    первый раз.

    Маркер зовёт только гейтвей после durable-резервации — это и есть
    доказательство, что исполнение живо. Поэтому попытка, которую сверка
    прогона успела перевести в FAILED_RETRYABLE/PARTIAL (сочла «зависшей»,
    пока цепочка исполнялась по claim за тик), маркером возвращается в
    LAUNCHING — так же, как её возвращает ``_prepare_city_launch`` на retry.
    BLOCKED и RECONCILING не принимаются: BLOCKED — неоднозначный инвентарь,
    RECONCILING — сверка идёт прямо сейчас и перепишет фазу.
    """
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.setdefault("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict) or attempt.get("phase") not in {
            "LAUNCHING",
            "FAILED_RETRYABLE",
            "PARTIAL",
        }:
            raise RuntimeError("City CREATE marker требует LAUNCHING attempt")
        plan = attempt.setdefault("city_plan", {}).get(city)
        if (
            not isinstance(plan, dict)
            or plan.get("media_manifest_sha256") != media_manifest_sha256
        ):
            raise RuntimeError("City CREATE marker manifest mismatch")
        if attempt.get("phase") != "LAUNCHING":
            logger.warning(
                "auto_launch: попытка %s была в %s при живом исполнении — "
                "маркер CREATE возвращает её в LAUNCHING",
                key,
                attempt.get("phase"),
            )
            attempt["phase"] = "LAUNCHING"
            # Вердикт сверки «CREATE не начинался» по этому городу устарел.
            errors = attempt.get("errors_by_city")
            if isinstance(errors, dict):
                errors.pop(city, None)
        now = datetime.now(_TZ_LOCAL)
        if not plan.get("create_started_at"):
            plan["create_started_at"] = now.isoformat()
            plan["reconcile_from"] = (now - timedelta(minutes=5)).isoformat()
        existing_until = _parse_fb_time(plan.get("reconcile_until"))
        marker_until = now + timedelta(hours=2)
        plan["reconcile_until"] = (
            max(existing_until, marker_until) if existing_until else marker_until
        ).isoformat()
        attempt["updated_at"] = now.isoformat()
        _save_auto_launch_state(state)


def _prepare_replacement_workflow_for_city(
    key: str,
    rec: dict,
    attempt: dict,
    city: str,
    adset_id: str,
    expected_ad_names: list[str],
    manifest_sha256: str,
) -> str | None:
    """Bind -> exact slot -> exact claim; обычный launch cleaner не получает."""
    replacement_config = _get_autopilot_config().get("replacement")
    if not isinstance(replacement_config, dict) or replacement_config.get("enabled") is not True:
        return None

    from services.cleanup_repository import get_cleanup_status
    from services.replacement_orchestrator import (
        bind_replacement_card,
        claim_waiting_workflow_for_launch,
        ensure_slot_for_workflow,
    )
    from services.replacement_workflow import get_replacement_launch, get_workflow

    account_kind = str(attempt.get("fb_account_kind") or "")
    account_id = str(attempt.get("fb_account_id") or "").removeprefix("act_")
    if account_kind not in {"offline", "online"} or not account_id:
        raise RuntimeError("Replacement launch требует exact account scope")

    card_id = str(rec["card_id"])
    launch_attempt_key = _replacement_launch_attempt_key(key, city, adset_id)

    snapshot = get_cleanup_status()
    rows = snapshot.get("replacement_workflows")
    if not isinstance(rows, list):
        raise RuntimeError("Replacement workflow snapshot некорректен")

    candidates: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Replacement workflow snapshot содержит invalid row")
        if row.get("city") != city or str(row.get("adset_id") or "") != adset_id:
            continue
        if row.get("phase") not in {"WAITING_SLOT", "WAITING_CARD", "LAUNCHING"}:
            continue
        workflow_id = str(row.get("workflow_id") or "")
        if not workflow_id:
            raise RuntimeError("Replacement workflow без workflow_id")
        link = get_replacement_launch(workflow_id)
        if link is None:
            if row.get("phase") == "WAITING_SLOT":
                candidates.append(row)
            continue
        try:
            durable_names = json.loads(str(link.get("expected_ad_names_json") or "[]"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Replacement launch link содержит invalid names") from exc
        if (
            link.get("launch_attempt_key") == launch_attempt_key
            and link.get("card_id") == card_id
            and link.get("city") == city
            and str(link.get("adset_id") or "") == adset_id
        ):
            if (
                link.get("media_manifest_sha256") != manifest_sha256
                or durable_names != expected_ad_names
            ):
                raise RuntimeError(
                    "Existing replacement launch binding не совпадает с immutable manifest"
                )
            candidates.append(row)

    if not candidates:
        return None
    if len(candidates) != 1:
        raise RuntimeError("Несколько replacement workflows подходят одному city launch")

    workflow_id = str(candidates[0]["workflow_id"])
    bind_replacement_card(
        workflow_id,
        card_id,
        str(rec.get("card_name") or ""),
        city,
        account_kind,  # type: ignore[arg-type]
        account_id,
        adset_id,
        expected_ad_names,
        len(expected_ad_names),
        manifest_sha256,
        launch_attempt_key,
    )
    _save_city_replacement_binding(
        key,
        city,
        workflow_id=workflow_id,
        launch_attempt_key=launch_attempt_key,
        media_manifest_sha256=manifest_sha256,
    )

    workflow = get_workflow(workflow_id)
    if not isinstance(workflow, dict):
        raise RuntimeError("Replacement workflow исчез после binding")
    if workflow.get("phase") == "WAITING_SLOT":
        slot = ensure_slot_for_workflow(workflow_id)
        if slot.action not in {"NO_DELETE_REQUIRED", "SLOTS_RELEASED"} or slot.deficit_after != 0:
            raise RuntimeError(f"Replacement slot не готов: {slot.reason or slot.action}")
        workflow = get_workflow(workflow_id)
    if not isinstance(workflow, dict):
        raise RuntimeError("Replacement workflow исчез после slot allocation")
    if workflow.get("phase") == "WAITING_CARD":
        claimed = claim_waiting_workflow_for_launch(workflow_id)
        if (
            not isinstance(claimed, dict)
            or claimed.get("workflow_id") != workflow_id
            or claimed.get("launch_attempt_key") != launch_attempt_key
        ):
            raise RuntimeError("Exact replacement launch claim не выполнен")
        workflow = claimed
    if workflow.get("phase") != "LAUNCHING":
        raise RuntimeError("Replacement workflow не находится в LAUNCHING")
    return workflow_id


def _record_replacement_city_success(key: str, city: str, ad_ids: list[str]) -> None:
    """Записывает exact IDs в SQLite раньше локального JSON ledger."""
    snapshot = _load_auto_launch_state()
    attempt = snapshot.get("launch_attempts", {}).get(key)
    if not isinstance(attempt, dict):
        raise KeyError(f"Попытка запуска {key} не найдена")
    plan = attempt.get("city_plan", {}).get(city)
    if not isinstance(plan, dict):
        return
    workflow_id = str(plan.get("replacement_workflow_id") or "")
    launch_attempt_key = str(plan.get("replacement_launch_attempt_key") or "")
    if not workflow_id and not launch_attempt_key:
        return
    if not workflow_id or not launch_attempt_key:
        raise RuntimeError("Неполная durable replacement binding в city plan")

    from services.replacement_orchestrator import record_workflow_launch_result

    record_workflow_launch_result(
        workflow_id,
        launch_attempt_key,
        city,
        ad_ids,
    )


def _record_city_success(key: str, city: str, ad_ids: list[str]) -> None:
    """Фиксирует подтверждённые CREATE ad_id и частичный launched_ever."""
    confirmed_ids = [
        str(ad_id).strip()
        for ad_id in ad_ids
        if ad_id is not None and str(ad_id).strip()
    ]
    if not confirmed_ids:
        raise ValueError("Нельзя записать успех города без ad_id")
    _record_replacement_city_success(key, city, confirmed_ids)
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.setdefault("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict):
            raise KeyError(f"Попытка запуска {key} не найдена")
        attempt.setdefault("ads_by_city", {})[city] = confirmed_ids
        attempt.setdefault("errors_by_city", {}).pop(city, None)
        attempt["pending_cities"] = [
            item for item in attempt.get("target_cities", [])
            if item not in attempt["ads_by_city"]
        ]
        now_iso = datetime.now(_TZ_LOCAL).isoformat()
        attempt["updated_at"] = now_iso

        card_id = str(attempt["card_id"])
        all_ids = [
            ad_id
            for target_city in attempt.get("target_cities", [])
            for ad_id in attempt["ads_by_city"].get(target_city, [])
        ]
        entry = {
            "at": attempt.get("first_started_at", attempt["started_at"]),
            "name": attempt.get("card_name", ""),
            "campaign_type": attempt.get("campaign_type", ""),
            "target_cities": list(attempt.get("target_cities", [])),
            "ads_by_city": dict(attempt["ads_by_city"]),
            "failed_by_city": dict(attempt.get("errors_by_city", {})),
            "ad_ids": all_ids,
            "complete": not attempt["pending_cities"],
            "completed_at": now_iso if not attempt["pending_cities"] else None,
        }
        state.setdefault("launched_ever", {})[card_id] = entry
        today = _get_today_str()
        if state.get("last_launch_date") != today:
            state["launched_today"] = []
        if card_id not in state.setdefault("launched_today", []):
            state["launched_today"].append(card_id)
        state["last_launch_date"] = today
        _save_auto_launch_state(state)


def _record_city_failure(key: str, city: str, error: str) -> None:
    """Сохраняет ошибку города, не убирая его из pending."""
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.setdefault("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict):
            raise KeyError(f"Попытка запуска {key} не найдена")
        attempt.setdefault("errors_by_city", {})[city] = _safe_error(error)
        attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
        _save_auto_launch_state(state)


def _finalize_launch_attempt(key: str) -> str:
    """Вычисляет итог попытки: полный успех, partial либо retryable failure."""
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.setdefault("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict):
            raise KeyError(f"Попытка запуска {key} не найдена")
        if attempt.get("phase") == "BLOCKED":
            return "BLOCKED"
        target = list(attempt.get("target_cities", []))
        succeeded = attempt.get("ads_by_city", {})
        pending = [city for city in target if not succeeded.get(city)]
        attempt["pending_cities"] = pending
        if not pending:
            phase = "SUCCEEDED"
            attempt["finished_at"] = datetime.now(_TZ_LOCAL).isoformat()
        elif succeeded:
            phase = "PARTIAL"
        else:
            phase = "FAILED_RETRYABLE"
        attempt["phase"] = phase
        attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
        _save_auto_launch_state(state)
        return phase


def _pending_cities(entry: dict, target_cities: list[str]) -> list[str]:
    """Возвращает только города без подтверждённых ad_id."""
    ads_by_city = entry.get("ads_by_city", {}) if isinstance(entry, dict) else {}
    return [city for city in target_cities if not ads_by_city.get(city)]


def _parse_fb_time(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("+0000", "+00:00").replace("Z", "+00:00"))
    except ValueError:
        return None


def _fetch_reconciliation_ads(
    account_kind: str,
    account_id: str,
    adset_ids: list[str] | None = None,
) -> list[dict]:
    """Читает ads строго из кабинета, зафиксированного до CREATE.

    Для маршрутизированного оффлайн-кабинета (не дефолтного) контекст обязан
    строиться через offline_account_context: голый offline-контекст активирует
    дефолтный кабинет, и fetch падает «активен другой account_id» — попытка
    уходила в BLOCKED при живых объявлениях во втором кабинете.

    ``adset_ids`` — адсеты городских планов: без серверного фильтра выборка
    читает весь кабинет и на больших кабинетах бьётся в потолок страниц.
    """
    from integrations.facebook import fetch_ads_for_launch_reconciliation
    from services.fb_token_provider import fb_account, offline_account_context

    if account_kind == "online":
        context_name = "online"
    else:
        context_name = offline_account_context(account_id)
    with fb_account(context_name):
        return fetch_ads_for_launch_reconciliation(
            expected_account_id=account_id,
            adset_ids=adset_ids,
        )


def _reconcile_launch_attempt(key: str, *, allow_blocked: bool = False) -> str:
    """Восстанавливает зависший LAUNCHING строго по adset+имени+окну.

    ``allow_blocked=True`` — явный повтор сверки для attempt в фазе BLOCKED
    (дренаж): раньше из BLOCKED не было автоматического выхода вообще, хотя
    причиной блокировки могла быть слепота сверки (один кабинет на попытку).
    Повторная сверка идемпотентна: не нашла — attempt вернётся в BLOCKED.
    """
    allowed_phases = {"LAUNCHING", "RECONCILING"}
    if allow_blocked:
        allowed_phases = allowed_phases | {"BLOCKED"}
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        attempt = state.setdefault("launch_attempts", {}).get(key)
        if not isinstance(attempt, dict):
            raise KeyError(f"Попытка запуска {key} не найдена")
        if attempt.get("phase") not in allowed_phases:
            return str(attempt.get("phase", "FAILED_RETRYABLE"))
        if attempt.get("phase") == "BLOCKED":
            # Прошлый вердикт сверки не должен пережить её повтор.
            errors = attempt.get("errors_by_city")
            if isinstance(errors, dict):
                errors.pop("__reconciliation__", None)
        attempt["phase"] = "RECONCILING"
        attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
        _save_auto_launch_state(state)

    try:
        account_kind = str(attempt.get("fb_account_kind", ""))
        default_account_id = str(attempt.get("fb_account_id", "")).removeprefix("act_")
        if account_kind not in {"offline", "online"} or not default_account_id:
            raise RuntimeError("В launch_attempt отсутствует зафиксированный FB кабинет")
        # Города одной карточки живут в разных кабинетах (карта роутинга
        # разносит L2/L1/CityF): кабинет каждого города берётся из его
        # city_plan, legacy-план без account_id наследует кабинет попытки.
        # Чтение одного кабинета на всю попытку было структурной слепотой:
        # объявления второго кабинета «не находились» и попытка блокировалась.
        city_accounts: dict[str, str] = {}
        adsets_by_account: dict[str, set[str]] = {}
        for city in list(attempt.get("pending_cities", [])):
            plan = attempt.get("city_plan", {}).get(city)
            plan_account = ""
            plan_adset = ""
            if isinstance(plan, dict):
                plan_account = str(plan.get("account_id") or "").removeprefix("act_")
                plan_adset = str(plan.get("target_adset_id") or "")
            city_account = plan_account or default_account_id
            city_accounts[city] = city_account
            if plan_adset:
                adsets_by_account.setdefault(city_account, set()).add(plan_adset)
        ads_by_account: dict[str, list[dict]] = {}
        for reconcile_account in sorted(set(city_accounts.values())):
            ads_by_account[reconcile_account] = _fetch_reconciliation_ads(
                account_kind,
                reconcile_account,
                sorted(adsets_by_account.get(reconcile_account, set())) or None,
            )
    except Exception as exc:
        safe_exc = _safe_error(exc)
        with _STATE_LOCK:
            state = _load_auto_launch_state()
            attempt = state["launch_attempts"][key]
            attempt["phase"] = "BLOCKED"
            attempt.setdefault("errors_by_city", {})["__reconciliation__"] = safe_exc
            attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
            _save_auto_launch_state(state)
        _send_telegram(f"🚨 Reconciliation запуска заблокирован: {safe_exc}")
        return "BLOCKED"

    with _STATE_LOCK:
        snapshot = _load_auto_launch_state()["launch_attempts"][key]
    # Владелец-слой отработал предложение: CONFIRMED-объявления уже записаны в попытку
    # (record_gateway_launch_result / сверщик), остальные доказанно без эффекта (волны I–II).
    # Тогда отсутствие объявлений — факт, а не «неизвестность», и частичный город — запущен
    # (решение владельца: объявления от прогона уже есть = город запущен).
    settled = _proposal_settled(str(snapshot.get("idempotency_key") or ""))
    blocked_reason: str | None = None
    recovered: dict[str, list[str]] = {}
    zero_match_cities: list[str] = []

    for city in list(snapshot.get("pending_cities", [])):
        plan = snapshot.get("city_plan", {}).get(city)
        if not isinstance(plan, dict):
            if settled:
                # Город даже не дошёл до staging в отработанном предложении — объявлений нет.
                zero_match_cities.append(city)
                continue
            blocked_reason = f"{city}: отсутствует durable city_plan после начала CREATE"
            break
        expected_names = list(plan.get("expected_ad_names", []))
        adset_id = str(plan.get("target_adset_id", ""))
        start = _parse_fb_time(plan.get("reconcile_from"))
        end = _parse_fb_time(plan.get("reconcile_until"))
        if not expected_names or start is None or end is None:
            blocked_reason = f"{city}: неполный durable city_plan"
            break
        city_account = city_accounts.get(city) or str(
            plan.get("account_id") or ""
        ).removeprefix("act_") or default_account_id
        ads = ads_by_account.get(city_account)
        if ads is None:
            blocked_reason = f"{city}: кабинет {city_account} не прочитан при reconciliation"
            break

        same_identity = [
            ad for ad in ads
            if str(ad.get("adset_id", "")) == adset_id
            and str(ad.get("name", "")) in expected_names
        ]
        inside: list[dict] = []
        outside = False
        for ad in same_identity:
            created = _parse_fb_time(ad.get("created_time"))
            if created is None or not (start <= created <= end):
                outside = True
            else:
                inside.append(ad)
        by_name = {
            name: [ad for ad in inside if str(ad.get("name", "")) == name]
            for name in expected_names
        }
        counts = [len(by_name[name]) for name in expected_names]
        if outside or any(count > 1 for count in counts):
            blocked_reason = f"{city}: неоднозначные совпадения exact name/adset/window"
            break
        if all(count == 1 for count in counts) and len(inside) == int(plan["expected_ads_count"]):
            recovered[city] = [str(by_name[name][0]["id"]) for name in expected_names]
        elif any(count > 0 for count in counts):
            if settled:
                recovered[city] = [str(by_name[name][0]["id"]) for name in expected_names if by_name[name]]
                continue
            blocked_reason = f"{city}: найден только частичный набор объявлений"
            break
        else:
            if plan.get("create_started_at") and settled:
                zero_match_cities.append(city)
                continue
            if plan.get("create_started_at"):
                blocked_reason = (
                    f"{city}: после CREATE_STARTED exact inventory не подтвердил "
                    "объявления; автоматический retry запрещён"
                )
                break
            zero_match_cities.append(city)

    if blocked_reason:
        with _STATE_LOCK:
            state = _load_auto_launch_state()
            attempt = state["launch_attempts"][key]
            attempt["phase"] = "BLOCKED"
            attempt.setdefault("errors_by_city", {})["__reconciliation__"] = blocked_reason
            attempt["updated_at"] = datetime.now(_TZ_LOCAL).isoformat()
            _save_auto_launch_state(state)
        _send_telegram(f"🚨 Reconciliation запуска заблокирован: {blocked_reason}")
        return "BLOCKED"

    for city, ad_ids in recovered.items():
        _record_city_success(key, city, ad_ids)
    for city in zero_match_cities:
        _record_city_failure(
            key,
            city,
            "reconciliation: CREATE не начинался, объявления не найдены",
        )
    if settled and zero_match_cities:
        # Маркер CREATE у города без объявлений после отработанного предложения — не улика:
        # иначе дренаж не даст ротировать ключ на дозапуск.
        with _STATE_LOCK:
            state = _load_auto_launch_state()
            plans = state["launch_attempts"][key].get("city_plan", {})
            for city in zero_match_cities:
                if isinstance(plans.get(city), dict):
                    plans[city].pop("create_started_at", None)
            _save_auto_launch_state(state)
    return _finalize_launch_attempt(key)


# ---------------------------------------------------------------------------
# Определение campaign_type по меткам карточки
# ---------------------------------------------------------------------------

def _detect_campaign_type(labels: list[str]) -> str:
    """Определяет тип кампании по меткам карточки Trello.

    PRODA → leadgen
    PRODB → leadgen_prodb
    Нет метки → leadgen (по умолчанию, как в существующем запуске)
    """
    for label in labels:
        # Проверяем точное совпадение (case-insensitive)
        label_upper = label.upper().strip()
        for key, campaign_type in _LABEL_TO_CAMPAIGN_TYPE.items():
            if key in label_upper:
                return campaign_type
    return _DEFAULT_CAMPAIGN_TYPE


# Онлайн-город в данных покрытия
_ONLINE_CITY = "Онлайн"

# campaign_type, допустимые для онлайн-пробела
_ONLINE_CAMPAIGN_TYPES = {"mql_online", "prodb_online"}

# campaign_type, допустимые для обычных (оффлайн) пробелов
# leadgen_prodb в обычный пробел НЕ назначаем — coverage не разделяет PRODA/PRODB,
# поэтому leadgen_prodb может уйти не туда. Безопаснее пропустить.
_OFFLINE_CAMPAIGN_TYPES = {"leadgen"}


def _campaign_type_fits_gap(campaign_type: str, city: str) -> bool:
    """Проверяет, подходит ли campaign_type для данного пробела (по городу).

    Правила:
    - пробел «Онлайн» → только mql_online / prodb_online
    - пробел любого другого города → только leadgen
      (leadgen_prodb исключён: coverage не различает PRODA/PRODB пробелы,
       нельзя гарантировать правильный матч)
    """
    if city == _ONLINE_CITY:
        return campaign_type in _ONLINE_CAMPAIGN_TYPES
    return campaign_type in _OFFLINE_CAMPAIGN_TYPES


# ---------------------------------------------------------------------------
# Основная логика решений
# ---------------------------------------------------------------------------

def decide_launches(coverage: dict, cards: list[dict]) -> list[dict]:
    """Возвращает ВСЕ незапущенные карточки-кандидаты для запуска (launch-all).

    Решение владельца: запуск льёт все готовые карточки, а пробелы
    покрытия больше НЕ гейтят запуск — они двигают ГЕНЕРАЦИЮ ТЗ
    (см. docs/product/fact-sheet-acme.md §7).

    Алгоритм:
    1. Исключаем карточки с темой-вето (_VETO_KEYWORD).
    2. Исключаем уже запущенные карточки (из state, launched_ever).
    3. Все оставшиеся → в рекомендации: cities из городских меток карточки
       (одна метка «CityA» = один город), без городских меток или с меткой
       «Все города» — cities=None (все города).

    coverage больше не используется для отбора. Темп ограничен дневным капом
    max_launches_per_day в run_auto_launch — единственный тормоз по бюджету.

    Args:
        coverage: результат analyze_coverage() (не используется для отбора)
        cards: список незапущенных карточек из get_unlaunched_cards()

    Returns:
        Список рекомендаций:
        [{card_id, card_name, campaign_type, cities, reason}, ...]
    """
    state = _load_auto_launch_state()
    already_launched = {
        card_id
        for card_id, entry in state.get("launched_ever", {}).items()
        if isinstance(entry, str) or (isinstance(entry, dict) and entry.get("complete") is True)
    }

    # Отфильтровываем карточки-кандидаты
    candidates = []
    for card in cards:
        card_id = card.get("id", "")
        card_name = card.get("name", "")
        labels = card.get("labels", [])

        # Исключаем уже запущенные через авто-запуск
        if card_id in already_launched:
            continue

        # Вето: тема _VETO_KEYWORD — никогда не запускать
        if _VETO_KEYWORD in card_name.lower():
            continue

        campaign_type = _detect_campaign_type(labels)
        label_cities = _cities_from_labels(labels)
        if label_cities is None:
            label_cities = _city_from_card_name(card_name)
        if label_cities == []:
            # Городские метки есть, но их города выключены из карты: запускать
            # некуда. Пропуск, а не «все города» (см. _cities_from_labels).
            logger.warning(
                "decide_launches: карточка «%s» (%s) пропущена — %s",
                card_name, card_id, _CITY_LABEL_UNROUTED_REASON,
            )
            continue
        candidates.append({
            "card_id": card_id,
            "card_name": card_name,
            "campaign_type": campaign_type,
            "labels": labels,
            "label_cities": label_cities,
        })

    if not candidates:
        return []

    # НОВОЕ поведение (решение владельца): запуск льёт ВСЕ готовые
    # карточки, а НЕ выбирает под пробелы покрытия. Пробелы теперь двигают
    # генерацию ТЗ (см. docs/product/fact-sheet-acme.md §7), а не запуск.
    # coverage больше не гейтит запуск. Темп ограничен дневным капом
    # max_launches_per_day в run_auto_launch — это единственный тормоз по бюджету.
    # cities=None → launch_single таргетит все города (карточки «Все города»);
    # городские метки сужают список до своих городов (исправленный баг: метка
    # «CityA» уходила во все города, включая CityF).
    recommendations = []
    for c in candidates:
        previous_entry = state.get("launched_ever", {}).get(c["card_id"])
        cities = c["label_cities"]
        if isinstance(previous_entry, dict) and previous_entry.get("complete") is False:
            # Ретрай: только города исходного контракта без ad_id. Контракт
            # (target_cities) уже сужен по меткам при первой попытке, поэтому
            # «висящих» городов вне меток не бывает.
            all_targets = previous_entry.get("target_cities") or (
                [_ONLINE_CITY]
                if c["campaign_type"] in _ONLINE_CAMPAIGN_TYPES
                else (cities or _standard_cities())
            )
            cities = _pending_cities(previous_entry, all_targets)
        recommendations.append({
            "card_id": c["card_id"],
            "card_name": c["card_name"],
            "campaign_type": c["campaign_type"],
            "cities": cities,
            "reason": "запуск готовой карточки",
            "labels": c["labels"],
        })

    return recommendations


# ---------------------------------------------------------------------------
# Запуск (dry_run или active)
# ---------------------------------------------------------------------------

def _get_autopilot_config():
    """Обёртка для мокирования в тестах."""
    from services.autopilot import get_autopilot_config
    return get_autopilot_config()


def _analyze_coverage():
    """Обёртка для мокирования в тестах."""
    from services.coverage_monitor import analyze_coverage
    return analyze_coverage()


def _get_done_list_id():
    """Обёртка для мокирования в тестах."""
    from integrations.trello import get_done_list_id
    return get_done_list_id()


def _get_unlaunched_cards(list_id: str):
    """Обёртка для мокирования в тестах."""
    from integrations.trello import get_unlaunched_cards
    return get_unlaunched_cards(list_id)


def _resumable_checked_cards(list_id: str, seen: set[str]) -> list[dict]:
    """Карточки «Готово» с галочкой, но с попыткой, ждущей дозапуска.

    Сверщик галочек ставил галочку по первому живому объявлению, и большинство частичных запусков
    выпадало из автозапуска. Trello не трогаем: такие карточки просто снова участвуют в прогоне.
    """
    resumable = resumable_card_ids() - seen
    if not resumable:
        return []
    try:
        from integrations.trello import get_open_cards

        return [
            {**card, "resume_checked": True}
            for card in get_open_cards(list_id)
            if str(card.get("id") or "") in resumable and card.get("dueComplete") is True
        ]
    except Exception as exc:  # noqa: BLE001 — дозапуск не должен ронять прогон
        logger.warning("auto_launch: карточки на дозапуск не прочитаны — %s", exc)
        return []


def _mark_card_done(card_id: str) -> None:
    """Обёртка Trello для мокирования и строгого вызова после полного успеха."""
    from integrations.trello import mark_card_done
    mark_card_done(card_id)


def _send_telegram(text: str, channel: str = "ads") -> bool:
    """Обёртка для мокирования в тестах."""
    from services.notifications import send_telegram
    return send_telegram(text, channel=channel)


def _extract_ad_ids_from_log(log: list[str]) -> dict[str, list[str]]:
    """Обёртка для мокирования в тестах (Фаза 4, журнал гипотез)."""
    from services.hypothesis_journal import extract_ad_ids_from_log
    return extract_ad_ids_from_log(log)


def _record_hypothesis(
    card_name: str,
    campaign_type: str,
    ad_ids_by_city: dict[str, list[str]],
    topic: dict | None,
) -> list[int]:
    """Обёртка для мокирования в тестах (Фаза 4, журнал гипотез)."""
    from services.hypothesis_journal import record_hypothesis
    return record_hypothesis(card_name, campaign_type, ad_ids_by_city, topic)


def _get_launch_checker(mode: str, *, prepared_media_by_card=None):
    """Единая runtime-фабрика checker-а; обёртка оставляет тесты offline."""
    from services.launch_checker_runtime import build_production_launch_checker

    return build_production_launch_checker(
        mode,
        prepared_media_by_card=prepared_media_by_card,
    )


def _auto_launch_source(source):
    from services.launch_checker import LaunchSource

    try:
        normalized = LaunchSource(source)
    except (TypeError, ValueError) as exc:
        raise ValueError("auto_launch source должен быть CRON или AUTO_LAUNCH_NOW") from exc
    if normalized not in {LaunchSource.CRON, LaunchSource.AUTO_LAUNCH_NOW}:
        raise ValueError("auto_launch source должен быть CRON или AUTO_LAUNCH_NOW")
    return normalized


def _card_sort_key(card: object) -> tuple[float, str]:
    """Сортирует malformed карточки последними; checker затем блокирует их."""
    if not isinstance(card, dict):
        return float("inf"), ""
    raw_pos = card.get("pos")
    try:
        pos = float(raw_pos)
    except (TypeError, ValueError):
        pos = float("inf")
    return pos, str(card.get("id") or "")


def _has_durable_incomplete(card: object, state: dict) -> bool:
    """Определяет schema-v2 partial/reconciliation без доверия legacy rows."""
    if not isinstance(card, dict):
        return False
    card_id = str(card.get("id") or "")
    entry = state.get("launched_ever", {}).get(card_id)
    if isinstance(entry, dict) and entry.get("complete") is False:
        return True
    attempts = state.get("launch_attempts", {})
    return any(
        isinstance(attempt, dict)
        and str(attempt.get("card_id") or "") == card_id
        and str(attempt.get("phase") or "").upper()
        in {"LAUNCHING", "RECONCILING", "PARTIAL", "FAILED_RETRYABLE"}
        for attempt in attempts.values()
    ) if isinstance(attempts, dict) else False


def _durable_blocked_attempt(card_id: str, state: dict) -> bool:
    attempts = state.get("launch_attempts", {})
    if not isinstance(attempts, dict):
        return True
    return any(
        isinstance(attempt, dict)
        and str(attempt.get("card_id") or "") == card_id
        and str(attempt.get("phase") or "").upper() == "BLOCKED"
        for attempt in attempts.values()
    )


def _reconcile_pending_cards(cards: list[object], state: dict) -> dict:
    """До новых reserve восстанавливает старые schema-v2 LAUNCHING attempts.

    Сверяются только осиротевшие попытки — те, чьё предложение уже прошло
    провайдера или умерло. Попытка с живым предложением (ждёт владельца,
    исполняется по claim за тик) исполняется гейтвеем, её фазу прогон не
    трогает. Не удалось прочитать БД — тоже не трогает (fail-closed).
    """
    attempts = state.get("launch_attempts", {})
    if not isinstance(attempts, dict):
        return state
    card_ids = {
        str(card.get("id") or "")
        for card in cards
        if isinstance(card, dict)
    }
    for key, attempt in list(attempts.items()):
        phase_now = str(attempt.get("phase") or "").upper() if isinstance(attempt, dict) else ""
        # BLOCKED сверяется повторно, только если предложение отработано: тогда отсутствие объявлений —
        # факт, и тупик «после CREATE_STARTED retry запрещён» снимается.
        retry_blocked = phase_now == "BLOCKED" and _proposal_settled(str(attempt.get("idempotency_key") or ""))
        if (
            not isinstance(attempt, dict)
            or str(attempt.get("card_id") or "") not in card_ids
            or (phase_now not in {"LAUNCHING", "RECONCILING"} and not retry_blocked)
        ):
            continue
        if retry_blocked:
            phase = _reconcile_launch_attempt(str(key), allow_blocked=True)
            if phase == "SUCCEEDED":
                _mark_card_done(str(attempt.get("card_id") or ""))
            continue
        in_flight = _attempt_in_flight(str(key), attempt)
        if in_flight is None:
            logger.warning(
                "auto_launch: сверка попытки %s пропущена — состояние предложения неизвестно",
                key,
            )
            continue
        if in_flight:
            logger.info(
                "auto_launch: попытка %s исполняется гейтвеем — сверка не нужна",
                key,
            )
            continue
        phase = _reconcile_launch_attempt(str(key))
        if phase == "SUCCEEDED":
            _mark_card_done(str(attempt.get("card_id") or ""))
    # Частичные попытки с отработанным предложением ротируются ДО гейта: иначе гейт видит
    # «частичный запуск требует сверки» и дозапуск не начинается никогда.
    for key, attempt in list(_load_auto_launch_state().get("launch_attempts", {}).items()):
        if (
            isinstance(attempt, dict)
            and str(attempt.get("card_id") or "") in card_ids
            and attempt_resumable(attempt)
            and _partial_rotation_allowed(attempt)
        ):
            _rotate_poisoned_attempt_key(str(key))
    return _load_auto_launch_state()


def _recommendation_from_card(card: dict) -> dict:
    labels = card.get("labels")
    normalized_labels = labels if isinstance(labels, list) else []
    return {
        "card_id": str(card.get("id") or ""),
        "card_name": str(card.get("name") or ""),
        "card_desc": str(card.get("desc") or ""),
        "campaign_type": _detect_campaign_type(normalized_labels),
        # Городские метки сужают запуск; [] = метки есть, города выключены —
        # прогон переводит такую карточку в отказ CITY_LABEL_UNROUTED.
        "cities": (
            _cities_from_labels(normalized_labels)
            if _cities_from_labels(normalized_labels) is not None
            else _city_from_card_name(card.get("name"))
        ),
        "reason": "запуск готовой карточки",
        "labels": normalized_labels,
        "pos": card.get("pos"),
    }


def _launch_check_request(rec: dict, source):
    from services.launch_checker import LaunchCheckRequest

    return LaunchCheckRequest(
        source=source,
        campaign_type=str(rec["campaign_type"]),
        cities=(tuple(rec["cities"]) if rec.get("cities") else None),
        as_carousel=False,
        actor="system:auto-launch",
    )


def _blocked_entry(card: dict, exc: Exception) -> dict:
    return {
        "card_id": str(card.get("id") or ""),
        "card_name": str(card.get("name") or ""),
        "check_id": getattr(exc, "check_id", None),
        "reason_codes": [str(getattr(exc, "code", "LAUNCH_CHECK_BLOCKED"))],
        # Причины идут в лог, state и Telegram — токены режем на входе.
        "reasons": [
            _safe_error(reason) for reason in getattr(exc, "reasons", (str(exc),))
        ],
    }


# Причина отказа в логе — до ~200 символов, в Telegram — до 120, строк в
# отчёте — не больше 10 (остальное сворачивается в «… и ещё K»).
_BLOCKED_LOG_REASON_LIMIT = 200
_BLOCKED_REPORT_REASON_LIMIT = 120
_BLOCKED_REPORT_MAX_LINES = 10


def _truncate_reason(text: object, limit: int) -> str:
    """Схлопывает пробелы/переносы и обрезает до limit символов с «…»."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 0)].rstrip() + "…"


def _blocked_codes(entry: dict) -> str:
    codes = [str(code) for code in (entry.get("reason_codes") or []) if str(code)]
    return ", ".join(codes) or "LAUNCH_CHECK_BLOCKED"


def _blocked_first_reason(entry: dict) -> str:
    reasons = [str(reason) for reason in (entry.get("reasons") or []) if str(reason)]
    return reasons[0] if reasons else "причина не указана"


def _log_blocked_entry(entry: dict) -> None:
    """Одна строка warning на заблокированную карточку: имя, id, коды, причины.

    Раньше в логе оставалось только «нет карточек, разрешённых launch checker»
    — по нему нельзя было понять, ПОЧЕМУ каждая карточка не прошла.
    """
    reasons = "; ".join(
        str(reason) for reason in (entry.get("reasons") or []) if str(reason)
    ) or "причина не указана"
    logger.warning(
        "auto_launch: карточка «%s» (%s) заблокирована: %s — %s",
        entry.get("card_name") or "без названия",
        entry.get("card_id") or "?",
        _blocked_codes(entry),
        _truncate_reason(reasons, _BLOCKED_LOG_REASON_LIMIT),
    )


def _blocked_report_lines(blocked: list[dict] | None) -> list[str]:
    """Строки для Telegram: «• имя — коды: первая причина» (HTML-экранировано).

    Не больше _BLOCKED_REPORT_MAX_LINES карточек, остаток — «… и ещё K».
    """
    if not blocked:
        return []
    lines: list[str] = []
    for entry in blocked[:_BLOCKED_REPORT_MAX_LINES]:
        name = str(entry.get("card_name") or entry.get("card_id") or "без названия")
        reason = _truncate_reason(
            _blocked_first_reason(entry), _BLOCKED_REPORT_REASON_LIMIT
        )
        lines.append(
            f"• {html.escape(name)} — {html.escape(_blocked_codes(entry))}: "
            f"{html.escape(reason)}"
        )
    rest = len(blocked) - _BLOCKED_REPORT_MAX_LINES
    if rest > 0:
        lines.append(f"… и ещё {rest}")
    return lines


def _record_last_run_blocked(mode: str, blocked: list[dict]) -> None:
    """Сохраняет в state срез заблокированных карточек последнего прогона.

    Его читает утренний дайджест (services/morning_digest.py): результат
    run_auto_launch больше нигде не сохраняется, а владельцу утром нужно
    видеть, какие карточки бот не пропустил и почему. Пишется на КАЖДОМ
    завершении прогона (пустой список = в последний раз блокировок не было).
    Сбой записи прогон не ломает.
    """
    snapshot = [
        {
            "card_id": str(entry.get("card_id") or ""),
            "card_name": str(entry.get("card_name") or ""),
            "reason_codes": [str(code) for code in (entry.get("reason_codes") or [])],
            "reason": _truncate_reason(
                _blocked_first_reason(entry), _BLOCKED_LOG_REASON_LIMIT
            ),
        }
        for entry in blocked
    ]
    try:
        with _STATE_LOCK:
            state = _load_auto_launch_state()
            state["last_run_at"] = datetime.now(_TZ_LOCAL).isoformat()
            state["last_run_mode"] = mode
            state["last_run_blocked"] = snapshot
            _save_auto_launch_state(state)
    except Exception as exc:
        logger.warning(
            "auto_launch: не удалось сохранить last_run_blocked — %s", _safe_error(exc)
        )


def _finish_checked_authorization(
    checked_plan,
    *,
    requested_outcome: str,
) -> object | None:
    """Compatibility-wrapper единого публичного lifecycle finalizer."""
    authorization = getattr(checked_plan, "authorization", None)
    if authorization is None:
        return None
    from services.launch_checker_runtime import finalize_launch_authorization

    return finalize_launch_authorization(
        authorization,
        requested_outcome=requested_outcome,
    )


def _record_daily_slot_from_finalization(
    card_id: str,
    finalization: object | None,
    today: str,
) -> None:
    """Пишет daily slot только при durable confirmed CREATE."""
    if not bool(getattr(finalization, "consumes_daily_slot", False)):
        return
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        if state.get("last_launch_date") != today:
            state["launched_today"] = []
        launched_today = state.setdefault("launched_today", [])
        if card_id not in launched_today:
            launched_today.append(card_id)
        state["last_launch_date"] = today
        _save_auto_launch_state(state)


def run_auto_launch(
    mode: str = "dry_run",
    max_launches: int = 1,
    *,
    source="CRON",
) -> dict:
    """Публичный вход с единым межпроцессным lock для active-режима."""
    try:
        normalized_source = _auto_launch_source(source)
    except ValueError as exc:
        return {
            "mode": mode,
            "recommendations": [],
            "launched": [],
            "blocked": [],
            "raw_count": 0,
            "eligible_count": 0,
            "blocked_count": 0,
            "skipped_reason": "неизвестный источник auto_launch",
            "error": str(exc),
        }
    if mode not in {"dry_run", "active"}:
        return {
            "mode": mode,
            "recommendations": [],
            "launched": [],
            "blocked": [],
            "raw_count": 0,
            "eligible_count": 0,
            "blocked_count": 0,
            "skipped_reason": "неизвестный режим auto_launch",
            "error": "mode должен быть dry_run или active",
        }
    if mode != "active":
        return _run_auto_launch_inner(
            mode=mode,
            max_launches=max_launches,
            source=normalized_source,
        )

    try:
        lease = _acquire_active_run_lease()
    except Exception as exc:
        safe_exc = _safe_error(exc)
        logger.error("run_auto_launch: active-run lease недоступен — %s", safe_exc)
        return {
            "mode": mode,
            "recommendations": [],
            "launched": [],
            "blocked": [],
            "raw_count": 0,
            "eligible_count": 0,
            "blocked_count": 0,
            "skipped_reason": "не удалось захватить безопасный active-run lease",
            "error": safe_exc,
        }
    if lease is None:
        return {
            "mode": mode,
            "recommendations": [],
            "launched": [],
            "blocked": [],
            "raw_count": 0,
            "eligible_count": 0,
            "blocked_count": 0,
            "skipped_reason": "auto_launch уже выполняется другим процессом",
            "error": None,
        }
    try:
        return _run_auto_launch_inner(
            mode=mode,
            max_launches=max_launches,
            source=normalized_source,
        )
    finally:
        _release_active_run_lease(lease)


def _run_auto_launch_inner(
    mode: str = "dry_run",
    max_launches: int = 1,
    *,
    source="CRON",
) -> dict:
    """Собирает рекомендации по авто-запуску и (при mode=active) выполняет их.

    ПРЕДОХРАНИТЕЛИ:
    - mode="dry_run" (ДЕФОЛТ): рекомендации + Telegram. Ничего не запускает.
    - mode="active": только при launch_enabled=True И enabled=True И kill_switch=False.
    - max_launches_per_day из конфига (дефолт 1) — cap на количество запусков.
    - Одна карточка запускается только один раз (трекается в state).

    Args:
        mode: "dry_run" (дефолт) или "active"
        max_launches: максимум запусков за вызов (≤ max_launches_per_day из конфига)

    Returns:
        {
            "mode": str,
            "recommendations": list,
            "launched": list,
            "skipped_reason": str | None,
            "error": str | None,
        }
    """

    result: dict = {
        "mode": mode,
        "recommendations": [],
        "launched": [],
        "proposals": [],
        "blocked": [],
        "raw_count": 0,
        "eligible_count": 0,
        "blocked_count": 0,
        "skipped_reason": None,
        "error": None,
    }

    observed_plans: dict[str, object] = {}

    try:
        # --- Получаем конфиг ---
        cfg = _get_autopilot_config()
        cfg_max = int(cfg.get("max_launches_per_day", 1))

        # --- Собираем данные ---
        _analyze_coverage()

        list_id = _get_done_list_id()
        raw_cards = list(_get_unlaunched_cards(list_id))
        raw_cards.extend(_resumable_checked_cards(list_id, {str(c.get("id") or "") for c in raw_cards}))
        cards = sorted(raw_cards, key=_card_sort_key)
        result["raw_count"] = len(cards)

        # Observe-preflight классифицирует весь raw snapshot и никогда не
        # создаёт reservations. Legacy/veto/partial остаются видимыми blocked,
        # а не исчезают из очереди как якобы уже запущенные.
        from services.launch_checker import CheckerMode, LaunchCheckBlocked

        observe_checker = _get_launch_checker(CheckerMode.OBSERVE.value)
        state_snapshot = _load_auto_launch_state()
        checker_mode = str((cfg.get("launch_checker") or {}).get("mode", "observe"))
        active_reconciliation_allowed = (
            mode == "active"
            and cfg.get("launch_enabled", False) is True
            and cfg.get("enabled", False) is True
            and cfg.get("kill_switch", False) is False
            and checker_mode == CheckerMode.ENFORCE.value
        )
        if active_reconciliation_allowed:
            state_snapshot = _reconcile_pending_cards(cards, state_snapshot)
        cards.sort(
            key=lambda card: (
                not _has_durable_incomplete(card, state_snapshot),
                _card_sort_key(card),
            )
        )
        recommendations: list[dict] = []
        candidate_cards: dict[str, dict] = {}
        for raw_card in cards:
            card = raw_card if isinstance(raw_card, dict) else {}
            rec = _recommendation_from_card(card)
            try:
                if rec["cities"] == []:
                    raise LaunchCheckBlocked(
                        "CITY_LABEL_UNROUTED",
                        (_CITY_LABEL_UNROUTED_REASON,),
                        None,
                    )
                if _durable_blocked_attempt(rec["card_id"], state_snapshot):
                    raise LaunchCheckBlocked(
                        "RECONCILIATION_REQUIRED",
                        ("Durable launch attempt заблокирован и требует ручной проверки",),
                        None,
                    )
                plan = observe_checker.prepare_and_reserve(
                    card,
                    _launch_check_request(rec, source),
                    state_snapshot,
                )
                _apply_skipped_targets(rec, plan)
            except LaunchCheckBlocked as exc:
                entry = _blocked_entry(card, exc)
                result["blocked"].append(entry)
                _log_blocked_entry(entry)
                continue
            observed_plans[rec["card_id"]] = plan
            candidate_cards[rec["card_id"]] = card
            recommendations.append(rec)

        result["recommendations"] = recommendations
        result["eligible_count"] = len(recommendations)
        result["blocked_count"] = len(result["blocked"])

        if not recommendations:
            result["skipped_reason"] = "нет карточек, разрешённых launch checker"
            _send_dry_run_telegram(
                [],
                _send_telegram,
                raw_count=result["raw_count"],
                eligible_count=0,
                blocked_count=result["blocked_count"],
                blocked=result["blocked"],
            )
            return result

        if mode == "dry_run":
            # Только рекомендации — ничего не запускаем
            _send_dry_run_telegram(
                recommendations,
                _send_telegram,
                raw_count=result["raw_count"],
                eligible_count=result["eligible_count"],
                blocked_count=result["blocked_count"],
                blocked=result["blocked"],
            )
            return result

        # --- mode="active" — проверяем все предохранители ---

        # Предохранитель 1: отдельный флаг launch_enabled (дефолт false)
        if not cfg.get("launch_enabled", False):
            result["skipped_reason"] = "launch_enabled=false в конфиге (авто-запуск выключен)"
            logger.info("run_auto_launch: пропущено — %s", result["skipped_reason"])
            _send_dry_run_telegram(
                recommendations,
                _send_telegram,
                raw_count=result["raw_count"],
                eligible_count=result["eligible_count"],
                blocked_count=result["blocked_count"],
                blocked=result["blocked"],
            )
            return result

        # Предохранитель 2: основной флаг enabled
        if not cfg.get("enabled", False):
            result["skipped_reason"] = "autopilot.enabled=false"
            logger.info("run_auto_launch: пропущено — %s", result["skipped_reason"])
            _send_dry_run_telegram(
                recommendations,
                _send_telegram,
                raw_count=result["raw_count"],
                eligible_count=result["eligible_count"],
                blocked_count=result["blocked_count"],
                blocked=result["blocked"],
            )
            return result

        # Предохранитель 3: kill_switch
        if cfg.get("kill_switch", False):
            result["skipped_reason"] = "kill_switch=true — аварийная остановка"
            logger.warning("run_auto_launch: заблокировано kill_switch")
            _send_dry_run_telegram(
                recommendations,
                _send_telegram,
                raw_count=result["raw_count"],
                eligible_count=result["eligible_count"],
                blocked_count=result["blocked_count"],
                blocked=result["blocked"],
            )
            return result

        if checker_mode != CheckerMode.ENFORCE.value:
            result["skipped_reason"] = "launch_checker.mode=observe — CREATE запрещён"
            _send_dry_run_telegram(
                recommendations,
                _send_telegram,
                raw_count=result["raw_count"],
                eligible_count=result["eligible_count"],
                blocked_count=result["blocked_count"],
                blocked=result["blocked"],
            )
            return result

        # Предохранитель 4: дневной лимит
        today = _get_today_str()
        state = _rollover_daily_state(today)
        partial_ids = {
            card_id
            for card_id, entry in state.get("launched_ever", {}).items()
            if isinstance(entry, dict) and entry.get("complete") is False
        }
        # Дозапуск недостающих городов не занимает дневной лимит (решение владельца).
        resume_ids = resumable_card_ids(state)
        # Schema-v2 partial/reconciliation идут первыми. Checker не позволит
        # им слепой CREATE, но они не могут быть вытеснены новыми карточками.
        recommendations.sort(
            key=lambda item: (
                item["card_id"] not in partial_ids,
                _card_sort_key({"pos": item.get("pos"), "id": item["card_id"]}),
            )
        )
        partial_count = sum(rec["card_id"] in partial_ids for rec in recommendations)
        launched_today = state.get("launched_today", [])
        if len(launched_today) >= cfg_max and partial_count == 0:
            result["skipped_reason"] = (
                f"дневной лимит исчерпан: {len(launched_today)}/{cfg_max} запусков сегодня"
            )
            logger.info("run_auto_launch: пропущено — %s", result["skipped_reason"])
            return result
        # Active producer лишь создаёт owner proposals. Ни provider CREATE,
        # ни Trello completion, ни verified daily counter здесь не выполняются.
        proposed_this_run = 0
        proposed_today = _rollover_proposed_today(today)
        for rec in recommendations:
            current_state = _rollover_daily_state(today)
            current_slots = [
                str(card_id) for card_id in current_state.get("launched_today", [])
            ]
            if rec["card_id"] in proposed_today:
                # Предложение по карточке уже ждёт владельца: второй раз его не
                # создаём и лишнюю подготовку запуска не делаем.
                continue
            # Дневной слот занимает и уже созданное предложение: иначе за сутки
            # владелец получал предложений больше дневного лимита, потому что
            # счётчик прогона обнулялся между запусками крона.
            occupied_slots = (set(current_slots) | set(proposed_today)) - resume_ids
            already_has_slot = rec["card_id"] in current_slots or rec["card_id"] in resume_ids
            effective_daily_slots = len(occupied_slots)
            if effective_daily_slots >= cfg_max and not already_has_slot:
                result["skipped_reason"] = (
                    f"дневной лимит исчерпан: {effective_daily_slots}/{cfg_max} "
                    "verified запусков и pending предложений"
                )
                break
            if proposed_this_run >= max_launches and not already_has_slot:
                break
            try:
                from agent.launcher import launch_single
                from services.approval_checker_models import ActionOrigin

                proposal_status = {
                    "running": True,
                    "log": [],
                    "progress": 0,
                    "total": 0,
                }
                # Staging AUTO_LAUNCH ищет durable attempt по idempotency_key
                # (prepare_staged_launch_for_gateway → _find_gateway_attempt), и
                # после одобрения по нему же находят attempt CREATE-шаги
                # (mark_gateway_create_started/record_gateway_launch_result).
                # Поэтому ключом proposal служит UUID4 самой durable попытки:
                # он стабилен между прогонами, значит повтор крона попадает в
                # тот же proposal, а не создаёт второй.
                target_cities = _target_cities(rec)
                attempt_key = _prepare_launch_attempt(rec, target_cities)
                # Дренаж отравленного ключа: терминальное предложение прошлых
                # дней не должно навсегда выбивать карточку из автоматизации.
                _rotate_poisoned_attempt_key(attempt_key)
                gateway_idempotency_key = str(
                    _load_auto_launch_state()["launch_attempts"][attempt_key].get(
                        "idempotency_key"
                    )
                    or ""
                )
                _gateway_attempt(attempt_key, gateway_idempotency_key)
                proposal_result = launch_single(
                    allow_checked=rec["card_id"] in resume_ids,
                    card_id=rec["card_id"],
                    card_name=rec["card_name"],
                    card_desc="",
                    status=proposal_status,
                    campaign_type=rec["campaign_type"],
                    cities=target_cities,
                    trello_labels=rec.get("labels"),
                    mark_done_on_complete=False,
                    idempotency_key=gateway_idempotency_key,
                    origin=ActionOrigin.AUTO_LAUNCH,
                )
                proposal_id = str(proposal_result.get("proposal_id") or "")
                if not proposal_id:
                    # launch_single гасит исключение и возвращает пустой dict,
                    # поэтому настоящую причину берём из его status.
                    reason = str(
                        proposal_status.get("error")
                        or proposal_status.get("outcome")
                        or "причина не указана"
                    )
                    raise RuntimeError(f"owner proposal не создан: {reason}")
                result["proposals"].append(
                    {
                        **rec,
                        "proposal_id": proposal_id,
                        "state": proposal_status.get("proposal_state"),
                    }
                )
                # Слот фиксируется durable сразу: следующий прогон крона за этот
                # же день обязан видеть занятый лимит, даже если упадёт ниже.
                if rec["card_id"] not in proposed_today:
                    proposed_today.append(rec["card_id"])
                _record_proposed_slot(rec["card_id"], today)
                proposed_this_run += 1
            except LaunchCheckBlocked as exc:
                entry = _blocked_entry(candidate_cards[rec["card_id"]], exc)
                if not any(
                    item.get("card_id") == rec["card_id"]
                    for item in result["blocked"]
                ):
                    result["blocked"].append(entry)
                    result["eligible_count"] = max(result["eligible_count"] - 1, 0)
                result["blocked_count"] = len(result["blocked"])
                _log_blocked_entry(entry)
            except Exception as exc:
                safe_exc = _safe_error(exc)
                logger.error(
                    "run_auto_launch: ошибка proposal карточки %s — %s",
                    rec["card_id"], safe_exc,
                )
                result["error"] = safe_exc

        # В отчёте очередь означает реально eligible остаток, а не raw Trello.
        _send_dry_run_telegram(
            recommendations,
            _send_telegram,
            raw_count=result["raw_count"],
            eligible_count=result["eligible_count"],
            blocked_count=result["blocked_count"],
            blocked=result["blocked"],
        )

    except Exception as exc:
        safe_exc = _safe_error(exc)
        logger.error("run_auto_launch: неожиданная ошибка — %s", safe_exc)
        result["error"] = safe_exc

    finally:
        # Срез отказов последнего прогона — для утреннего дайджеста. Пишем до
        # чистки медиа: запись сама никогда не бросает, а чистка — может.
        _record_last_run_blocked(mode, result["blocked"])
        if observed_plans:
            from services.launch_checker_runtime import cleanup_prepared_media

            for plan in observed_plans.values():
                cleanup_prepared_media(plan.media)

    return result


def _execute_launch(
    rec: dict,
    state: dict,
    today: str,
    *,
    checked_plan=None,
) -> None:
    """Гарантирует terminal audit даже при сбое до входа в launcher."""
    from services.launch_checker import LaunchCheckBlocked

    requested_outcome = "FAILED"
    try:
        _execute_launch_impl(
            rec,
            state,
            today,
            checked_plan=checked_plan,
        )
        requested_outcome = "COMPLETED"
    except LaunchCheckBlocked:
        requested_outcome = "BLOCKED"
        raise
    finally:
        finalization = _finish_checked_authorization(
            checked_plan,
            requested_outcome=requested_outcome,
        )
        _record_daily_slot_from_finalization(
            str(rec.get("card_id") or ""),
            finalization,
            today,
        )


def _execute_launch_impl(
    rec: dict,
    state: dict,
    today: str,
    *,
    checked_plan=None,
) -> None:
    """Запускает pending-города карточки через durable launch attempt.

    До CREATE пишется точный city_plan. ``launched_ever`` появляется только
    после подтверждённых ad_id. Зависший ``LAUNCHING`` сначала reconciles,
    поэтому повторный CREATE вслепую невозможен.
    """
    from agent.launcher import launch_single
    from services.approval_checker_models import ActionOrigin
    from services.launch_checker import LaunchCheckBlocked

    card_id = rec["card_id"]
    card_name = rec["card_name"]
    campaign_type = rec["campaign_type"]
    authorization = getattr(checked_plan, "authorization", None)
    if checked_plan is not None:
        request = getattr(checked_plan, "request", None)
        if (
            str(getattr(checked_plan, "card_id", "")) != str(card_id)
            or str(getattr(request, "campaign_type", "")) != str(campaign_type)
            or authorization is None
        ):
            raise LaunchCheckBlocked(
                "AUTHORIZATION_SCOPE_DRIFT",
                ("Checked plan не совпадает с auto launch record",),
                getattr(checked_plan, "check_id", None),
            )
    target_cities = _target_cities(rec)
    key = _prepare_launch_attempt(rec, target_cities)

    durable_state = _load_auto_launch_state()
    attempt = durable_state["launch_attempts"][key]
    # При partial-retry rec.cities содержит только missing, но durable attempt
    # остаётся источником полного исходного контракта всех target cities.
    target_cities = list(attempt.get("target_cities", target_cities))
    if attempt.get("phase") == "SUCCEEDED":
        _mark_card_done(card_id)
        state.clear()
        state.update(durable_state)
        return

    attempt = _ensure_attempt_account_for_create(key, campaign_type)
    gateway_idempotency_key = str(attempt.get("idempotency_key") or "")
    _gateway_attempt(key, gateway_idempotency_key)
    if attempt.get("phase") in {"LAUNCHING", "RECONCILING"}:
        # Живое предложение по этому же ключу исполняет гейтвей: ни сверять,
        # ни запускать второй раз нельзя. Неизвестность БД здесь не
        # fail-closed: этот путь сам создаёт объявления и сверка до него —
        # прежний контракт (legacy прямое исполнение, не боевой прогон).
        if _attempt_in_flight(key, attempt) is True:
            _finish_checked_authorization(
                checked_plan,
                requested_outcome="RELEASED",
            )
            raise RuntimeError(
                "Запуск уже исполняется гейтвеем по живому предложению; "
                "сверка и повторный CREATE запрещены"
            )
        phase = _reconcile_launch_attempt(key)
        if phase == "BLOCKED":
            _finish_checked_authorization(
                checked_plan,
                requested_outcome="BLOCKED_RECONCILE",
            )
            raise RuntimeError("Запуск заблокирован: reconciliation неоднозначен")
        durable_state = _load_auto_launch_state()
        attempt = durable_state["launch_attempts"][key]
    if attempt.get("phase") == "SUCCEEDED":
        _mark_card_done(card_id)
        state.clear()
        state.update(durable_state)
        return

    attempt = _ensure_attempt_account_for_create(key, campaign_type)
    _start_retry_cycle(key)
    durable_state = _load_auto_launch_state()
    attempt = durable_state["launch_attempts"][key]

    pending_cities = _pending_cities(attempt, target_cities)
    if not pending_cities:
        _finish_checked_authorization(
            checked_plan,
            requested_outcome="RELEASED",
        )
        raise RuntimeError("Нет pending-городов, но попытка не завершена")

    logger.info(
        "run_auto_launch: запускаю карточку '%s' (type=%s, cities=%s)",
        card_name, campaign_type, pending_cities,
    )

    # Минимальный статус-объект для launch_single
    status: dict = {
        "running": True,
        "current": card_name,
        "progress": 0,
        "total": len(pending_cities),
        "step": "",
        "step_pct": None,
        "log": [],
    }

    launch_result = None
    try:
        # Legacy checker proof только освобождаем. Provider execution
        # получает новый permit только в sealed action gateway.
        _finish_checked_authorization(
            checked_plan,
            requested_outcome="RELEASED",
        )
        launch_result = launch_single(
            card_id=card_id,
            card_name=card_name,
            card_desc="",
            status=status,
            tenant_id=None,
            campaign_type=campaign_type,
            cities=pending_cities,
            as_carousel=False,
            trello_labels=rec.get("labels"),
            mark_done_on_complete=False,
            idempotency_key=gateway_idempotency_key,
            origin=ActionOrigin.AUTO_LAUNCH,
        )
    except LaunchCheckBlocked:
        _finish_checked_authorization(
            checked_plan,
            requested_outcome="BLOCKED",
        )
        current_attempt = _load_auto_launch_state()["launch_attempts"][key]
        if current_attempt.get("phase") == "PREPARED":
            for city in pending_cities:
                _record_city_failure(key, city, "launch checker/provider заблокировал CREATE")
            _finalize_launch_attempt(key)
        raise
    except Exception:
        _finish_checked_authorization(
            checked_plan,
            requested_outcome="RELEASED",
        )
        current_attempt = _load_auto_launch_state()["launch_attempts"][key]
        # Если city_plan ещё не был сохранён, CREATE точно не начинался и
        # попытку можно безопасно оставить retryable. LAUNCHING сохраняем для
        # обязательной reconciliation на следующем прогоне.
        if current_attempt.get("phase") == "PREPARED":
            for city in pending_cities:
                _record_city_failure(key, city, "launch_single завершился исключением до CREATE")
            _finalize_launch_attempt(key)
        raise
    finally:
        latest = _load_auto_launch_state()
        state.clear()
        state.update(latest)

    operation_id = str(status.get("operation_id") or "")
    if operation_id:
        rec["operation_id"] = operation_id

    # Разбираем ad_id из лога ОДИН раз — используется и журналом гипотез,
    # и Контролем запуска (services/launch_verify.py читает ad_ids из state).
    ad_ids_by_city = _extract_ad_ids_from_log(status.get("log", []))
    if isinstance(launch_result, dict):
        for city, raw_ids in launch_result.items():
            ids = raw_ids if isinstance(raw_ids, list) else [raw_ids]
            ad_ids_by_city[city] = [
                str(ad_id).strip()
                for ad_id in ids
                if ad_id is not None and str(ad_id).strip()
            ]

    latest_attempt = _load_auto_launch_state()["launch_attempts"][key]
    already_recorded = latest_attempt.get("ads_by_city", {})
    for city, ad_ids in ad_ids_by_city.items():
        if city in pending_cities and ad_ids and not already_recorded.get(city):
            _record_city_success(key, city, ad_ids)

    # Если для неуспешного города уже был сохранён city_plan, CREATE мог
    # частично пройти (например, asset 1 создан, asset 2 упал). Сначала живой
    # exact reconciliation; обычный retry до неё запрещён.
    current_attempt = _load_auto_launch_state()["launch_attempts"][key]
    planned_pending = [
        city for city in current_attempt.get("pending_cities", [])
        if city in current_attempt.get("city_plan", {})
        and not current_attempt.get("ads_by_city", {}).get(city)
    ]
    if planned_pending and current_attempt.get("phase") == "LAUNCHING":
        reconcile_phase = _reconcile_launch_attempt(key)
        if reconcile_phase == "BLOCKED":
            _finish_checked_authorization(
                checked_plan,
                requested_outcome="BLOCKED_RECONCILE",
            )
            raise RuntimeError("Запуск заблокирован: partial CREATE требует ручной проверки")

    for city in pending_cities:
        current = _load_auto_launch_state()["launch_attempts"][key]
        if not current.get("ads_by_city", {}).get(city):
            _record_city_failure(key, city, "объявление не создано")

    phase = _finalize_launch_attempt(key)
    if phase == "BLOCKED":
        _finish_checked_authorization(
            checked_plan,
            requested_outcome="BLOCKED_RECONCILE",
        )
        raise RuntimeError("Запуск заблокирован reconciliation")

    # Прикрепляем данные запуска к rec — отчёт _send_active_telegram использует их
    # для метки продукта/городов/кнопки Стоп. rec передан по ссылке и лежит в
    # result["launched"] (run_auto_launch), поэтому мутация видна отправителю.
    rec["ad_ids_by_city"] = ad_ids_by_city

    # --- Контроль запуска (докладка ad_id в state) ---
    # Плоский список всех ad_id этой карточки (без разбивки по городам) —
    # launch_verify проверяет их живым FB-запросом днём, не дожидаясь ночного
    # backfill в creative_kb. Пишем ПОСЛЕ launch_single (ad_id уже существуют),
    # но ДО журнала гипотез — сбой журнала не должен терять уже собранные ad_id.
    final_attempt = _load_auto_launch_state()["launch_attempts"][key]
    combined_by_city = final_attempt.get("ads_by_city", {})
    rec["ad_ids_by_city"] = dict(combined_by_city)
    all_ad_ids = [
        ad_id
        for city in target_cities
        for ad_id in combined_by_city.get(city, [])
    ]
    rec["ad_ids"] = all_ad_ids
    if all_ad_ids:
        with _STATE_LOCK:
            latest_state = _load_auto_launch_state()
            # Карта id->ad_ids для кнопки Стоп (свой ретеншн, вечный ledger не трогаем).
            _record_launch_stop(latest_state, card_id, card_name, all_ad_ids)
            _save_auto_launch_state(latest_state)
        state.clear()
        state.update(latest_state)

    if phase == "SUCCEEDED":
        # Authorization завершается раньше Trello: UI не увидит full success,
        # пока durable provider claims не подтверждают каждый expected ad_id.
        _finish_checked_authorization(
            checked_plan,
            requested_outcome="COMPLETED",
        )
        _mark_card_done(card_id)
    else:
        _finish_checked_authorization(
            checked_plan,
            requested_outcome="PARTIAL" if all_ad_ids else "RELEASED",
        )
        errors = [line for line in status.get("log", []) if "❌" in line]
        raise RuntimeError(f"Частичный запуск, pending={_pending_cities(final_attempt, target_cities)}: {errors}")

    # --- Журнал гипотез (Фаза 4, см. docs/specs/ARCH-phase4-hypothesist.md) ---
    # Запуск важнее журнала: сбой регистрации гипотезы НЕ должен ронять
    # уже успешный запуск карточки — только предупреждение в лог.
    try:
        hypothesist_cfg = _get_autopilot_config().get("hypothesist") or {}
        if hypothesist_cfg.get("enabled", True):
            topic = rec.get("topic")
            _record_hypothesis(card_name, campaign_type, ad_ids_by_city, topic)
    except Exception as exc:
        logger.warning(
            "run_auto_launch: не удалось зарегистрировать гипотезу для карточки '%s' — %s",
            card_name, exc,
        )


# ---------------------------------------------------------------------------
# Человеческий формат отчёта (склонения, блок запуска, сборщики сообщения)
# ---------------------------------------------------------------------------

def _plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    """Русское склонение по числу. forms=(one, few, many).

    Примеры: _plural_ru(1, ("город","города","городов")) -> "город"
             _plural_ru(3, ...) -> "города"; _plural_ru(5, ...) -> "городов".
    Правила: n%10==1 и n%100!=11 -> one; n%10 in 2..4 и n%100 not in 12..14 -> few;
    иначе many. Отрицательные/0 -> many.
    """
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return forms[1]
    return forms[2]


def _cities_line(rec: dict) -> str:
    """Строка про города/объявления запуска.

    Есть rec["ad_ids_by_city"] ({city: [ad_id,...]}): "K городов · M объявлений",
    и при 1<=K<=5 добавляет "\nГорода: c1, c2, ...". Иначе (None/пусто) фолбэк на
    rec["cities"] или "все города" — чтобы dry-run и None-safety не падали.
    HTML-экранирует названия городов.
    """
    ad_ids_by_city = rec.get("ad_ids_by_city")
    if ad_ids_by_city:
        cities = list(ad_ids_by_city.keys())
        k = len(cities)
        m = sum(len(ids) for ids in ad_ids_by_city.values())
        cities_word = _plural_ru(k, ("город", "города", "городов"))
        ads_word = _plural_ru(m, ("объявление", "объявления", "объявлений"))
        line = f"{k} {cities_word} · {m} {ads_word}"
        if 1 <= k <= 5:
            escaped_cities = ", ".join(html.escape(str(c)) for c in cities)
            line += f"\nГорода: {escaped_cities}"
        return line

    # Фолбэк для dry-run/старых вызовов без ad_ids_by_city — используем rec["cities"]
    cities_fallback = rec.get("cities")
    if cities_fallback:
        cities_str = ", ".join(html.escape(str(c)) for c in cities_fallback)
    else:
        cities_str = "все города"
    return f"Города: {cities_str}"


def _launch_block(rec: dict) -> str:
    """Один блок запуска для отчёта (HTML, без кнопок).

    Формат (каждая часть на своей строке):
        {tag} <b>{name}</b>
        {cities_line}
        {budget_line}
    tag — метка продукта из classify_product(name, "", labels) через
    PRODUCTS[product]["tag"] (например "[PRODA]"). name — rec["card_name"] БЕЗ
    обрезки, html.escape. budget_line: если rec.get("daily_budget_usd") не None —
    f'Бюджет: {fmt_money(x, "$")}/день', иначе честная
    'Бюджет: по настройкам кампании (адсет не меняли)'.
    """
    name = rec.get("card_name") or "без названия"
    labels = rec.get("labels") or []
    product = classify_product(name, "", labels)
    tag = PRODUCTS[product]["tag"]

    budget = rec.get("daily_budget_usd")
    if budget is not None:
        budget_line = f'Бюджет: {fmt_money(budget, "$")}/день'
    else:
        budget_line = "Бюджет: по настройкам кампании (адсет не меняли)"

    return f"{tag} <b>{html.escape(str(name))}</b>\n{_cities_line(rec)}\n{budget_line}"


def _build_active_report(
    launched: list[dict],
    remaining_in_queue: int | None,
    *,
    raw_count: int | None = None,
    eligible_count: int | None = None,
    blocked_count: int | None = None,
    blocked: list[dict] | None = None,
) -> tuple[str, list[list[tuple[str, str]]]]:
    """Возвращает (HTML-текст отчёта о реальном запуске, матрица inline-кнопок).

    text: заголовок «🚀 Запустил N ... (из «Готово»)», затем блок на каждый
    запуск, затем футер (очередь / суммарный бюджет / оценка через 48 часов).
    blocked: записи _blocked_entry — после «Заблокировано: N» идут строки
    «• имя — коды: первая причина» (до 10, остальное — «… и ещё K»).
    buttons: по одному ряду на каждый запуск, у которого есть card_id и ad_id,
    и callback_data укладывается в 64 байта. Подпись кнопки обрезается по
    границе слова (truncate_at_word_boundary) — обрубков посреди слова быть
    не должно (владелец против).
    """
    n = len(launched)
    header_noun = _plural_ru(n, ("новую рекламу", "новые рекламы", "новых реклам"))
    lines = [f"🚀 <b>Запустил {n} {header_noun}</b> (из «Готово»)"]

    for rec in launched:
        lines.append(_launch_block(rec))

    footer_lines: list[str] = []
    if remaining_in_queue is not None:
        queue_noun = _plural_ru(remaining_in_queue, ("карточка", "карточки", "карточек"))
        footer_lines.append(f'📦 В очереди «Готово»: {remaining_in_queue} {queue_noun}')
    if raw_count is not None and eligible_count is not None and blocked_count is not None:
        footer_lines.append(
            f"📊 Всего: {raw_count} · Допущено checker-ом: {eligible_count} · "
            f"Заблокировано: {blocked_count}"
        )
    footer_lines.extend(_blocked_report_lines(blocked))

    # Суммарный добавленный бюджет — ТОЛЬКО если у ВСЕХ запусков известна сумма
    # (иначе показывать частичную сумму — соврать про масштаб добавленного бюджета).
    budgets = [rec.get("daily_budget_usd") for rec in launched]
    if launched and all(b is not None for b in budgets):
        footer_lines.append(f'💰 Добавлено в день: {fmt_money(sum(budgets), "$")}')

    footer_lines.append(
        "⏱ Первая оценка — через 48 часов (ранние сигналы), решения автопилота — в обычных окнах"
    )
    lines.append("\n".join(footer_lines))

    text = "\n\n".join(lines)

    buttons: list[list[tuple[str, str]]] = []
    for rec in launched:
        card_id = rec.get("card_id")
        ad_ids = rec.get("ad_ids") or []
        if not card_id or not ad_ids:
            continue
        callback_data = f"stop_launch:{card_id}"
        if len(callback_data.encode("utf-8")) > 64:
            continue
        name = str(rec.get("card_name") or card_id)
        # Обрезка по границе слова, а не посреди слова (name[:22] раньше давал
        # обрубки типа «Креатор А / Длинная те» — владелец против такого).
        label = f"⏸ Остановить «{truncate_at_word_boundary(name, 22)}»"
        buttons.append([(label, callback_data)])

    return text, buttons


def _build_dry_run_report(
    recommendations: list[dict],
    *,
    raw_count: int | None = None,
    eligible_count: int | None = None,
    blocked_count: int | None = None,
    blocked: list[dict] | None = None,
) -> str:
    """HTML-текст плана авто-запуска (без кнопок, с пометкой «(план)»).

    Пусто -> "🤖 <b>Авто-запуск (план)</b>\n\nНет готовых карточек для запуска."
    Иначе: заголовок + блок на каждую рекомендацию (cities_line даст «Города:
    все города», бюджет — честная строка).
    blocked: записи _blocked_entry — сразу под «Заблокировано: N» идут строки
    «• имя — коды: первая причина» (до 10, остальное — «… и ещё K»), чтобы
    владелец видел, ПОЧЕМУ карточка не прошла, а не только сколько их.
    """
    counts_line = None
    if raw_count is not None and eligible_count is not None and blocked_count is not None:
        counts_line = (
            f"📊 Всего: {raw_count} · Можно запустить: {eligible_count} · "
            f"Заблокировано: {blocked_count}"
        )
    counts_block = "\n".join(
        line for line in (counts_line, *_blocked_report_lines(blocked)) if line
    ) or None
    if not recommendations:
        lines = ["🤖 <b>Авто-запуск (план)</b>", "Нет готовых карточек для запуска."]
        if counts_block:
            lines.append(counts_block)
        return "\n\n".join(lines)

    lines = ["🚀 <b>Авто-запуск (план)</b>"]
    for rec in recommendations:
        lines.append(_launch_block(rec))
    if counts_block:
        lines.append(counts_block)
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram-уведомления
# ---------------------------------------------------------------------------

def _send_with_buttons(text: str, buttons: list[list[tuple[str, str]]]) -> bool:
    """Обёртка над telegram_bot.send_with_buttons (ленивый импорт — для мока в тестах
    и во избежание цикла импортов). Возвращает True при успехе."""
    from services.telegram_bot import send_with_buttons
    return send_with_buttons(text, buttons)


def _send_active_telegram(
    launched: list[dict],
    send_telegram_fn,
    remaining_in_queue: int | None = None,
    *,
    raw_count: int | None = None,
    eligible_count: int | None = None,
    blocked_count: int | None = None,
    blocked: list[dict] | None = None,
) -> None:
    """Отправляет отчёт о реальных запусках. Позиционная сигнатура НЕ меняется
    (обратная совместимость вызова из run_auto_launch и тестов); blocked —
    необязательный список отказов для строк «• имя — коды: причина».

    Логика: если launched пуст -> return. Строим (text, buttons). Если buttons есть —
    шлём через _send_with_buttons; при False/исключении — фолбэк send_telegram_fn(text,
    channel="ads"). Если buttons нет — сразу send_telegram_fn. Ошибки ловим, логируем
    type(exc).__name__, не бросаем наружу.
    """
    if not launched:
        return

    try:
        text, buttons = _build_active_report(
            launched,
            remaining_in_queue,
            raw_count=raw_count,
            eligible_count=eligible_count,
            blocked_count=blocked_count,
            blocked=blocked,
        )

        if not buttons:
            send_telegram_fn(text, channel="ads")
            return

        sent_ok = False
        try:
            sent_ok = _send_with_buttons(text, buttons)
        except Exception as exc:
            logger.warning("_send_active_telegram: send_with_buttons упал — %s", type(exc).__name__)

        if not sent_ok:
            send_telegram_fn(text, channel="ads")
    except Exception as exc:
        logger.warning("_send_active_telegram: ошибка отправки — %s", type(exc).__name__)


def _send_dry_run_telegram(
    recommendations: list[dict],
    send_telegram_fn,
    *,
    raw_count: int | None = None,
    eligible_count: int | None = None,
    blocked_count: int | None = None,
    blocked: list[dict] | None = None,
) -> None:
    """Отправляет план (dry_run). Позиционная сигнатура НЕ меняется. Строит текст
    через _build_dry_run_report и шлёт send_telegram_fn(text, channel="ads").
    blocked — необязательный список отказов (см. _build_dry_run_report). Ошибки
    не бросает наружу.
    """
    try:
        text = _build_dry_run_report(
            recommendations,
            raw_count=raw_count,
            eligible_count=eligible_count,
            blocked_count=blocked_count,
            blocked=blocked,
        )
        send_telegram_fn(text, channel="ads")
    except Exception as exc:
        logger.warning("_send_dry_run_telegram: ошибка отправки — %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Stop-map: карта «id запуска → ad_ids» для кнопки «⏸ Остановить» в отчёте
# ---------------------------------------------------------------------------

def _prune_stop_map(stop_map: dict, now: datetime) -> None:
    """In-place чистка stop_map: удаляет записи старше STOP_MAP_RETENTION_DAYS
    (по полю 'at', ISO). Битую дату не трогает. Затем обрезает до MAX_STOP_ENTRIES,
    оставляя самые свежие по 'at'."""
    cutoff = now - timedelta(days=STOP_MAP_RETENTION_DAYS)

    for card_id in list(stop_map.keys()):
        entry = stop_map.get(card_id)
        at_raw = entry.get("at") if isinstance(entry, dict) else None
        if not at_raw:
            continue
        try:
            at_dt = datetime.fromisoformat(str(at_raw))
        except ValueError:
            continue
        if at_dt < cutoff:
            del stop_map[card_id]

    if len(stop_map) <= MAX_STOP_ENTRIES:
        return

    def _sort_key(item):
        _, entry = item
        at_raw = entry.get("at") if isinstance(entry, dict) else None
        try:
            return datetime.fromisoformat(str(at_raw))
        except (ValueError, TypeError):
            # Записи без валидной даты считаем самыми старыми — уйдут первыми
            return datetime.min.replace(tzinfo=_TZ_LOCAL)

    freshest_ids = {
        card_id
        for card_id, _ in sorted(stop_map.items(), key=_sort_key, reverse=True)[:MAX_STOP_ENTRIES]
    }
    for card_id in list(stop_map.keys()):
        if card_id not in freshest_ids:
            del stop_map[card_id]


def _record_launch_stop(
    state: dict,
    card_id: str,
    name: str,
    ad_ids: list[str],
    now: datetime | None = None,
) -> None:
    """Мутирует state['stop_map'] IN-PLACE: {card_id: {"name","ad_ids","stopped":False,"at":iso}}.
    Затем _prune_stop_map. Сохранение на диск делает вызывающий код
    (_execute_launch/run_auto_launch через _save_auto_launch_state) — поэтому пишем в
    ТОТ ЖЕ in-memory state, который они сохраняют (не отдельный файл-save — иначе
    финальный _save_auto_launch_state в run_auto_launch затрёт stop_map).

    KNOWN LIMITATION (гонка, задокументирована как _execute_apply в telegram_bot):
    если владелец кликнет «Стоп» по старому запуску РОВНО во время нового прогона
    run_auto_launch, финальный in-memory _save может откатить флаг stopped. Импакт
    минимален: объявления в FB всё равно на паузе (idempotent), локальный флаг
    восстановится повторным кликом. Приемлемо (≤3 запуска/сут)."""
    now = now or datetime.now(_TZ_LOCAL)
    stop_map = state.setdefault("stop_map", {})
    stop_map[card_id] = {
        "name": name,
        "ad_ids": list(ad_ids),
        "stopped": False,
        "at": now.isoformat(),
    }
    _prune_stop_map(stop_map, now)


def get_launch_stop_entry(card_id: str) -> dict | None:
    """Под _STATE_LOCK читает копию stop_map[card_id] из файла. None если нет.
    Формат: {"name": str, "ad_ids": [str,...], "stopped": bool, "at": iso}."""
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        entry = state.get("stop_map", {}).get(card_id)
        if entry is None:
            return None
        return dict(entry)


def mark_launch_stopped(card_id: str) -> bool:
    """Под _STATE_LOCK: stop_map[card_id]['stopped']=True + save. True если запись
    была, False если карточки нет в stop_map."""
    with _STATE_LOCK:
        state = _load_auto_launch_state()
        stop_map = state.get("stop_map", {})
        entry = stop_map.get(card_id)
        if entry is None:
            return False
        entry["stopped"] = True
        state["stop_map"] = stop_map
        _save_auto_launch_state(state)
        return True
