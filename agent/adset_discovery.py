"""Автообнаружение FB адсетов по имени.
Заменяет хардкод config.py:ADSETS — сама находит свежие адсеты.

Оффлайн-скан мультикабинетный: обходит ВСЕ кабинеты карты
маршрутизации (services/launch_routing.py). Карта работает парой
(город, тип): L2 расщеплённых городов живёт в «ACME cabinet_b», их L1 и MQL — в
cabinet_a (config.FB_ACCOUNT_ID), CityF целиком в cabinet_b.

Адсет, найденный в кабинете, куда его пара НЕ маршрутизирована (спящие
L2-дубли расщеплённых городов в cabinet_a, запаркованная CityF там же, дубль L1
в cabinet_b), игнорируется с warning — в результат попадает только инвентарь
«своего» кабинета. Именно этот фильтр, а не статус адсета, отделяет живой
инвентарь от спящего: PAUSED-дубли в cabinet_a никто не удаляет.

Боевой целью может стать только адсет ЖИВОЙ кампании
(campaign.effective_status == ACTIVE, см. _campaign_is_live): ACTIVE-адсет
в выключенной кампании не показывается, и запуск в него — тихая потеря
креатива. Пара без единого живого кандидата выпадает из карты (fail-closed).

PRODB-адсеты («Owner | PRODB | MQL | Geo <Город> | ver1», тип PRODB)
всех городов карты — боевой выбор из cabinet_b: пара (город, PRODB) есть в карте
роутинга, роутинг-фильтр её пропускает, и _pick_primaries кладёт адсет в
leadgen[city]["PRODB"]. Без этой пары в карте PRODB-адсеты уезжают в
old, а PRODB-карточки (leadgen_prodb) резолвятся по языку в PRODA-адсеты.
Статический fallback (config.ADSETS) типа PRODB не знает — без живого discovery
PRODB-запуск откажет fail-closed, а не уйдёт в PRODA.
"""

import re
import time
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Кеш в памяти, per-кабинет (TTL ниже).
# Ключи: "offline:<account_id>:<отпечаток карты>" — по кабинету карты роутинга
#        (отпечаток в ключе: правка settings обесценивает кеш сразу, а не через
#        полчаса, иначе «переопределение без деплоя» не работает);
#        "online" / "bot" — одиночные аккаунты по активному контексту.
_CACHE: dict = {}  # {cache_key: {"data": ..., "ts": ...}}
# 30 мин: адсеты меняются медленно, экономим FB rate limit.
# Если новый адсет не виден — сбросить через ?force_refresh=true.
_TTL_SECONDS = 1800

# Поддерживаемые города
# CityF в списке обязательна: без неё _classify_adset возвращает
# (None, None), и адсет города НЕВИДИМ для запуска — новые креативы
# туда не доезжают, город доедает старую статику.
# Одно время город изымался из распознавания (мешал одно-кабинетному
# запуску), возвращён вместе с маршрутизацией город→кабинет: запаркованные
# cabinet_a-адсеты CityF отсеивает мультикабинетный скан (город берётся
# только из своего кабинета по services/launch_routing.py).
CITIES = ["CityA", "CityB", "CityC", "CityD", "CityE", "CityF"]


def _classify_adset(name: str, adset: dict = None) -> tuple[Optional[str], Optional[str]]:
    """По имени (и доп. полям) адсета определяет (city, type).
    type: "L2" | "L1" | "MQL" | "PRODB" — L2/L1 для Lead Gen, MQL для Website
    MQL, PRODB для PRODB-адсетов («| PRODB |» в имени).
    Возвращает (None, None) если не классифицируется.

    Поведение зависит от FB_MODE:
      offline — городские адсеты с пайпами (`Owner | L2 | ...`)
      online  — единый псевдогород "Онлайн", классификация через destination_type/имя
    """
    if not name:
        return None, None

    # Активный аккаунт thread имеет приоритет над глобальным FB_MODE.
    # Это позволяет одновременно работать с двумя FB-аккаунтами в проекте:
    # - оффлайн (cabinet_a) — основной flow, классификация по городам
    # - онлайн (Acme, через fb_account("online")) — псевдогород "Онлайн"
    try:
        from services.fb_token_provider import get_active_account
        active = get_active_account()
    except Exception:
        active = None

    try:
        from config import FB_MODE
    except ImportError:
        FB_MODE = "offline"

    if active == "online" or FB_MODE == "online":
        return _classify_online(name, adset or {})

    # ── offline (старая логика) ─────────────────────────────────────
    # Псевдогород "Онлайн" — MQL_Online адсеты в оффлайн-кабинете.
    # Имя адсета: "Онлайн L2 | Instagram | MQL_Online | ...".
    # Эти адсеты — website-кампания на сайт компании (config.WEBSITE_LANDING_URL), но мы их группируем
    # как отдельный "город" Онлайн, чтобы в дашборде была отдельная вкладка.
    if "MQL_Online" in name or "MQL_online" in name or name.startswith("Онлайн "):
        if "Онлайн L2" in name or re.search(r"\|\s*L2\s*\|", name):
            return "Онлайн", "L2"
        if "Онлайн L1" in name or re.search(r"\|\s*L1\s*\|", name):
            return "Онлайн", "L1"

    # Поиск города в имени (регистронезависимо)
    city = None
    name_lower = name.lower()
    for c in CITIES:
        if c.lower() in name_lower:
            city = c
            break
    if not city:
        return None, None

    # PRODB-адсеты («Owner | PRODB | MQL | Geo <Город> | ver1») — отдельный тип.
    # Проверка ДО MQL-маркеров: в имени есть "MQL", и без ранней ветки такие
    # адсеты либо падали в MQL, либо вообще не классифицировались —
    # их объявления не попадали в adset_map/creative_kb, и автопилот не видел
    # сливов PRODB-адсетов.
    if re.search(r"\|\s*PRODB\s*\|", name, re.IGNORECASE):
        return city, "PRODB"

    # MQL Website — содержит характерные маркеры
    if any(marker in name for marker in [
        "Instagram | MQL", "WEBSITE | MQL", "MQL-CAPI",
        "WEBSITE", "(MQL-CAPI)",
    ]):
        return city, "MQL"

    # Lead Gen L2/L1 — ищем " | L2 |" или " | L1 |"
    if re.search(r"\|\s*L2\s*\|", name):
        return city, "L2"
    if re.search(r"\|\s*L1\s*\|", name):
        return city, "L1"

    return None, None


def _classify_online(name: str, adset: dict) -> tuple[Optional[str], Optional[str]]:
    """Онлайн-режим: один псевдогород 'Онлайн', тип по имени + destination_type.

    Тип адсета определяется так:
    - LeadGen L2/L1  — destination_type=ON_AD + optimization_goal=LEAD_GENERATION
                       + язык из имени по маркеру-токену (l2 → L2, l1 → L1)
    - MQL website    — destination_type=UNDEFINED + optimization_goal=OFFSITE_CONVERSIONS
                       (это website-кампания на сайт компании)
    - Прочие dest_type (INSTAGRAM_PROFILE, WHATSAPP, MESSAGING_*) — игнорируем
    """
    dest = (adset.get("destination_type") or "").upper()
    goal = (adset.get("optimization_goal") or "").upper()

    # Lead Gen формы FB
    if dest == "ON_AD" and goal in ("LEAD_GENERATION", "QUALITY_LEAD"):
        lang = _detect_adset_lang(name)
        if lang:
            return "Онлайн", lang
        return None, None

    # Website (MQL) — трафик на сайт
    if goal in ("OFFSITE_CONVERSIONS", "LANDING_PAGE_VIEWS", "LINK_CLICKS"):
        # Только если в имени упомянут сайт/landing/MQL — иначе это может быть
        # просто трафик на профиль и т.п.
        nm = name.lower()
        if any(m in nm for m in ("сайт", "site", "site", "website", "mql", "landing", "лендинг")):
            return "Онлайн", "MQL"

    return None, None


def _configured_l2_markers() -> tuple[str, ...]:
    """Маркеры L2 из config.L2_MARKERS (env L2_MARKERS) — те же, что у карточек."""
    try:
        from config import L2_MARKERS
    except ImportError:
        return ()
    return tuple(sorted(str(marker).strip().lower() for marker in L2_MARKERS if str(marker).strip()))


# Маркеры языка в имени онлайн-адсета — отдельные токены, регистр не важен:
# «L2/ видео 1», «Видео | l2 | v1», «[L1] Лид-форма». L2 — второй язык
# кабинета, L1 — основной. Язык определяется ТОЛЬКО явным маркером (не по
# алфавиту текста). «l2»/«l1» работают всегда; дополнительные маркеры L2
# настраиваются через config.L2_MARKERS.
L2_NAME_MARKERS: tuple[str, ...] = tuple(dict.fromkeys(("l2", *_configured_l2_markers())))
L1_NAME_MARKERS: tuple[str, ...] = ("l1",)

_MARKER_SEPARATORS = r"[\s/|()\[\],\-—.]"


def _marker_pattern(markers: tuple[str, ...]) -> re.Pattern:
    """Регэксп «маркер как отдельный токен» по разделителям /|()[],-—. и пробелу."""
    alternatives = "|".join(re.escape(marker) for marker in markers)
    return re.compile(
        rf"(?:^|{_MARKER_SEPARATORS})(?:{alternatives})(?:$|{_MARKER_SEPARATORS})",
        re.IGNORECASE,
    )


_LANG_PATTERNS_L2 = _marker_pattern(L2_NAME_MARKERS)
_LANG_PATTERNS_L1 = _marker_pattern(L1_NAME_MARKERS)


def _starts_with_marker(name: str, markers: tuple[str, ...]) -> bool:
    """Имя начинается с маркера и разделителя: «L2/ …» или «L2 …»."""
    head = name.lower()
    return any(
        head.startswith(f"{marker.lower()}/") or head.startswith(f"{marker.lower()} ")
        for marker in markers
    )


def _detect_adset_lang(name: str) -> Optional[str]:
    """Определяет язык адсета онлайна по имени: L2 / L1 / None."""
    # Префикс "L2/" / "L1/" — самый частый паттерн в онлайне
    if _starts_with_marker(name, L2_NAME_MARKERS):
        return "L2"
    if _starts_with_marker(name, L1_NAME_MARKERS):
        return "L1"
    # Поиск как отдельного токена в имени
    if _LANG_PATTERNS_L2.search(name):
        return "L2"
    if _LANG_PATTERNS_L1.search(name):
        return "L1"
    return None


def _fetch_account_adsets(account_id: str) -> list[dict]:
    """Тянет все адсеты кабинета с пагинацией (без кеша, кидает при ошибке).

    Токен берётся из активного thread-контекста (get_fb_token): оффлайн
    FB_TOKEN имеет ads_management в обоих кабинетах карты роутинга.
    """
    from services.fb_token_provider import get_fb_token
    from agent.fb_common import session, API

    token = get_fb_token()
    all_adsets: list[dict] = []
    url = f"{API}/act_{account_id}/adsets"
    params = {
        # destination_type/optimization_goal нужны для онлайн-режима.
        # campaign{effective_status} — гейт живости кампании в _pick_primaries.
        "fields": "id,name,status,created_time,destination_type,optimization_goal,"
                  "campaign{id,name,effective_status}",
        "limit": 100,
        "access_token": token,
    }
    next_url = None
    while True:
        r = session.get(next_url or url, params=None if next_url else params, timeout=30)
        if not r.ok:
            logger.warning("FB API discover_adsets failed (act_%s): %s", account_id, r.text[:200])
            raise RuntimeError(f"FB API act_{account_id}: {r.status_code}")
        data = r.json()
        all_adsets.extend(data.get("data", []))
        next_url = (data.get("paging") or {}).get("next")
        if not next_url:
            break
        # Safety: не больше 1000 адсетов
        if len(all_adsets) > 1000:
            break
    logger.info("Discovered %d adsets from FB API (act_%s)", len(all_adsets), account_id)
    return all_adsets


def _group_classified(all_adsets: list[dict]) -> dict:
    """Группирует классифицированные адсеты в {leadgen, mql, old}.

    В каждой группе (city, type) главный — самый свежий ACTIVE адсет,
    остальные уходят в old (для исторической аналитики).
    """
    by_city_type: dict = {}
    for adset in all_adsets:
        city, atype = _classify_adset(adset.get("name", ""), adset)
        if not city or not atype:
            continue
        by_city_type.setdefault((city, atype), []).append(adset)
    return _pick_primaries(by_city_type)


def _campaign_is_live(adset: dict) -> bool:
    """Кампания адсета живая (effective_status == ACTIVE).

    Fail-closed: нет данных кампании — адсет боевой целью не становится.
    Статус самого адсета тут ни при чём: ACTIVE-адсет в PAUSED-кампании
    показов не даёт, и запуск в него — тихая потеря креатива (регрессия:
    пара (город, L2) разрешалась в ACTIVE-адсет давно выключенной
    кампании).
    """
    campaign = adset.get("campaign")
    if not isinstance(campaign, dict):
        return False
    return campaign.get("effective_status") == "ACTIVE"


def _pick_primaries(by_city_type: dict) -> dict:
    """Из групп (city, type) выбирает primary-адсеты, остальные — в old.

    Боевым выбором (leadgen/mql) может стать только адсет живой кампании
    (_campaign_is_live); группа целиком из мёртвых кампаний primary не даёт —
    пара выпадает из карты, и запуск по ней откажет fail-closed вместо тихого
    создания объявлений там, где они не показываются. Отсеянные адсеты уходят
    в old: историческая разметка (build_adset_map) статусов не различает.
    """
    leadgen: dict = {}
    mql: dict = {}
    old: dict = {}

    for (city, atype), adsets in by_city_type.items():
        # Сортировка: сначала ACTIVE, потом по created_time desc (самый свежий первый)
        def _sort_key(a: dict) -> tuple:
            status_rank = 0 if a.get("status") == "ACTIVE" else 1
            ts = 0
            ct = a.get("created_time")
            if ct:
                try:
                    ts = datetime.fromisoformat(ct.replace("+0000", "+00:00")).timestamp()
                except Exception:
                    pass
            return (status_rank, -ts)

        live = [a for a in adsets if _campaign_is_live(a)]
        dead = [a for a in adsets if not _campaign_is_live(a)]
        adsets_sorted = sorted(live, key=_sort_key) + sorted(dead, key=_sort_key)

        if live:
            primary_id = adsets_sorted[0]["id"]
            if atype == "MQL":
                mql[city] = primary_id
            else:
                leadgen.setdefault(city, {})[atype] = primary_id
            rest = adsets_sorted[1:]
        else:
            campaigns = sorted({
                str((a.get("campaign") or {}).get("name") or "<без кампании>")
                for a in adsets
            })
            logger.warning(
                "discover_adsets: у пары %s/%s нет адсетов в живой кампании "
                "(кандидатов %d, кампании: %s) — пара исключена из карты запуска",
                city, atype, len(adsets), ", ".join(campaigns),
            )
            rest = adsets_sorted

        # Остальные — в old (для исторической аналитики)
        if rest:
            old.setdefault(city, {}).setdefault(atype, [])
            old[city][atype] = [a["id"] for a in rest]

    return {"leadgen": leadgen, "mql": mql, "old": old}


def _group_offline_account(
    all_adsets: list[dict],
    account_id: str,
    route_table: dict,
    default_account: str,
) -> dict:
    """Классифицирует адсеты одного оффлайн-кабинета с роутинг-фильтром.

    Fail-closed против неоднозначности «один инвентарь в двух кабинетах».
    Решение принимается по паре (город, тип), потому что один
    город живёт в двух кабинетах: L2 CityA — в cabinet_b, L1 и MQL — в cabinet_a.
    Четыре ветки:
    - пара маршрутизирована сюда → принимаем;
    - пара маршрутизирована в другой кабинет → warning + игнор (сюда попадают
      спящие L2-дубли расщеплённых городов в cabinet_a и запаркованная CityF);
    - пара не маршрутизирована, но город управляемый → warning + игнор; без
      этой ветки немаршрутизированный тип известного города провалился бы в
      ветку псевдогородов и был бы принят из cabinet_a — fail-open ровно там, где
      мы чиним тихий промах;
    - город вне карты (псевдогород «Онлайн») → принимаем только из дефолтного
      кабинета (cabinet_a), как до мультикабинетного скана.

    Отсеянные адсеты не пропадают: их id уезжают в ``old``. Фильтр обязан резать
    БОЕВОЙ выбор (leadgen/mql), но не историческую разметку — по ``old`` строится
    карта adset_id → (город, тип) в agent/fb_common.build_adset_map, и объявление
    с неизвестным adset_id синкуется с пустыми city/adset_type, ЗАТИРАЯ прежнюю
    разметку (creative_backfill: ON CONFLICT … SET city = excluded.city). Выброси
    спящие L2-адсеты cabinet_a совсем — и вся докризисная статистика L2 расщеплённых городов
    потеряла бы город при первом же пересинке.
    """
    by_city_type: dict = {}
    historical: dict = {}
    for adset in all_adsets:
        city, atype = _classify_adset(adset.get("name", ""), adset)
        if not city or not atype:
            continue
        routed_account = route_table.get((city, atype))
        if routed_account is not None:
            if routed_account != account_id:
                logger.warning(
                    "discover_adsets: адсет «%s» (%s) — %s/%s — найден в act_%s, "
                    "но пара маршрутизирована в act_%s — только история",
                    adset.get("name", ""), adset.get("id", "?"),
                    city, atype, account_id, routed_account,
                )
                historical.setdefault((city, atype), []).append(adset)
                continue
        elif _is_managed_city(route_table, city):
            logger.warning(
                "discover_adsets: адсет «%s» (%s) — %s/%s — найден в act_%s, "
                "но тип %s города %s не маршрутизирован ни в один кабинет — только история",
                adset.get("name", ""), adset.get("id", "?"),
                city, atype, account_id, atype, city,
            )
            historical.setdefault((city, atype), []).append(adset)
            continue
        elif account_id != default_account:
            logger.warning(
                "discover_adsets: адсет «%s» (%s) города %s вне карты роутинга "
                "найден в act_%s — принимаю такие города только из act_%s, только история",
                adset.get("name", ""), adset.get("id", "?"),
                city, account_id, default_account,
            )
            historical.setdefault((city, atype), []).append(adset)
            continue
        by_city_type.setdefault((city, atype), []).append(adset)
    grouped = _pick_primaries(by_city_type)
    _append_historical(grouped, historical)
    return grouped


def _append_historical(grouped: dict, historical: dict) -> None:
    """Дописывает отсеянные роутингом адсеты в ``old`` (только разметка истории).

    В ``leadgen``/``mql`` они не попадают никогда — туда их и не пускает фильтр;
    ``old`` же читает только историческая аналитика (build_adset_map), которой
    важно знать город и тип адсета независимо от того, в каком он кабинете.
    """
    for (city, atype), adsets in historical.items():
        ids = [adset["id"] for adset in adsets if adset.get("id")]
        if not ids:
            continue
        bucket = grouped["old"].setdefault(city, {}).setdefault(atype, [])
        bucket.extend(adset_id for adset_id in ids if adset_id not in bucket)


def _is_managed_city(route_table: dict, city: str) -> bool:
    """Город присутствует в переданной карте хотя бы одним типом."""
    return any(known_city == city for known_city, _ in route_table)


def _merge_account_group(merged: dict, grouped: dict, account_id: str) -> None:
    """Вливает группу одного кабинета в общий результат, помня кабинет пары.

    Город больше не принадлежит кабинету целиком: у CityA L2 приезжает из
    cabinet_b, а L1 и MQL — из cabinet_a, поэтому словари сливаются по типу, а не
    заменяются. После роутинг-фильтра пары кабинетов не пересекаются; защита
    ниже — от гонки со сменой settings между итерациями (первый выигрывает).
    """
    def _claim(city: str, adset_type: str) -> bool:
        known = merged["accounts"].get(city, {}).get(adset_type)
        if known is not None and known != account_id:
            logger.error(
                "discover_adsets: %s/%s уже взят из act_%s, дубль из act_%s отброшен",
                city, adset_type, known, account_id,
            )
            return False
        merged["accounts"].setdefault(city, {})[adset_type] = account_id
        return True

    for city, types in grouped["leadgen"].items():
        for adset_type, adset_id in types.items():
            if _claim(city, adset_type):
                merged["leadgen"].setdefault(city, {})[adset_type] = adset_id
    for city, adset_id in grouped["mql"].items():
        if _claim(city, "MQL"):
            merged["mql"][city] = adset_id
    # old вливается из ЛЮБОГО кабинета и объединяется: это исторический слой
    # разметки (build_adset_map), а не боевой выбор. Спящие L2-адсеты расщеплённых
    # городов лежат в cabinet_a, тогда как сама пара маршрутизирована в cabinet_b —
    # условие «только свой кабинет» выбросило бы их и обнулило city/adset_type
    # у всей докризисной статистики при ближайшем синке.
    for city, types in grouped["old"].items():
        for adset_type, ids in types.items():
            bucket = merged["old"].setdefault(city, {}).setdefault(adset_type, [])
            bucket.extend(adset_id for adset_id in ids if adset_id not in bucket)


def discover_adsets(force_refresh: bool = False) -> dict:
    """Возвращает {
        "leadgen": {city: {"L2": adset_id, "L1": adset_id, "PRODB": adset_id}, ...},
                   # PRODB — PRODB-адсет города (cabinet_b); есть
                   # только у городов с живым PRODB-адсетом
        "mql":     {city: adset_id, ...},
        "old":     {city: {"L2": [old_ids], "L1": [old_ids], "PRODB": [...]}, ...},
        "accounts": {city: {"L2": account_id, "L1": ..., "MQL": ..., "PRODB": ...}, ...},
                   # кабинет КАЖДОЙ ПАРЫ (без act_): город живёт
                   # в двух кабинетах сразу (L2 в cabinet_b, L1/MQL в cabinet_a)
        "discovered_at": timestamp ISO,
        "source": "fb_api" | "cache" | "fallback",
    }

    Оффлайн — мультикабинетный скан по launch_routing.accounts_to_scan()
    с кешем per-кабинет. source="fb_api" только когда ВСЕ кабинеты опрошены
    живьём; частичный отказ любого кабинета = fallback на config (fail-closed:
    частичный результат не притворяется полным).

    При FB rate limit / ошибке — fallback на config.py:ADSETS / ADSETS_MQL.
    """
    # Имя активного аккаунта (None/"offline" или "online"/"bot")
    from services.fb_token_provider import get_active_account
    active = get_active_account() or "offline"

    try:
        from config import FB_MODE
    except ImportError:
        FB_MODE = "offline"

    if active == "online" or FB_MODE == "online":
        return _discover_single_account("online", force_refresh)
    if active != "offline":
        # Например "bot" — одиночный скан кабинета активного контекста.
        return _discover_single_account(active, force_refresh)
    return _discover_offline_routed(force_refresh)


def _discover_offline_routed(force_refresh: bool) -> dict:
    """Оффлайн-скан всех кабинетов карты роутинга, merge per-город."""
    from config import FB_ACCOUNT_ID

    default_account = str(FB_ACCOUNT_ID).removeprefix("act_")
    merged: dict = {"leadgen": {}, "mql": {}, "old": {}, "accounts": {}}
    served_from_cache = 0
    try:
        from services.launch_routing import (
            accounts_to_scan,
            get_route_table,
            routing_fingerprint,
        )

        route_table = get_route_table()
        # Отпечаток карты в ключе кеша: группировка зависит от карты, и после
        # правки settings старый ключ просто не находится — пересбор сразу,
        # а не через оставшийся TTL.
        fingerprint = routing_fingerprint()[:8]
        for account_id in accounts_to_scan():
            cache_key = f"offline:{account_id}:{fingerprint}"
            cached = _CACHE.get(cache_key)
            if not force_refresh and cached and (time.time() - cached["ts"]) < _TTL_SECONDS:
                grouped = cached["data"]
                served_from_cache += 1
            else:
                raw = _fetch_account_adsets(account_id)
                grouped = _group_offline_account(
                    raw, account_id, route_table, default_account
                )
                # Записи этого кабинета с прежним отпечатком карты больше не
                # найдутся по ключу — выбрасываем, иначе в демоне они копятся
                # с каждой правкой settings и не освобождаются до рестарта.
                stale_prefix = f"offline:{account_id}:"
                for key in [
                    key
                    for key in _CACHE
                    if key.startswith(stale_prefix) and key != cache_key
                ]:
                    del _CACHE[key]
                _CACHE[cache_key] = {"data": grouped, "ts": time.time()}
            _merge_account_group(merged, grouped, account_id)

        # Проверка: если обнаружено 0 адсетов для leadgen — fallback на config
        if not merged["leadgen"]:
            logger.warning("discover_adsets: leadgen пуст — fallback на config")
            raise RuntimeError("Ни одного классифицированного leadgen адсета не найдено")

        result = {
            **merged,
            "discovered_at": datetime.now().isoformat(),
            "source": "cache" if served_from_cache else "fb_api",
        }

        # Логируем расхождения с хардкодом config
        _log_config_diff(merged["leadgen"], merged["mql"])

        return result

    except Exception as e:
        logger.warning("discover_adsets fallback на config: %s", e)
        return _config_fallback(default_account)


def _config_fallback(default_account: str) -> dict:
    """Статический fallback из config, отфильтрованный картой роутинга.

    config.ADSETS описывает ТОЛЬКО cabinet_a, причём для L2 расщеплённых городов там лежат
    id спящих адсетов, оставшихся после миграции L2 в cabinet_b. Отдать их конвейеру
    значит тихо слить креатив в PAUSED-адсет — ровно тот отказ, который чиним.
    Поэтому в БОЕВОЙ выбор fallback попадают только пары, чей маршрут ведёт в
    дефолтный кабинет; остальные (L2 расщеплённых городов, вся CityF) отсутствуют, и
    их запуск без живого discovery откажет ниже по конвейеру (fail-closed).

    Отсеянные id при этом уезжают в ``old`` — тем же правилом, что и в живом
    скане: ``old`` читает только историческая разметка (build_adset_map), и без
    неё синк объявления затирает city/adset_type пустыми.
    """
    from config import ADSETS as STATIC_ADSETS, ADSETS_MQL as STATIC_MQL

    try:
        from services.launch_routing import get_route_table

        route_table = get_route_table()
    except Exception as exc:  # noqa: BLE001 — карта нечитаема = ничего не отдаём
        logger.error(
            "discover_adsets: карта роутинга недоступна в fallback (%s) — "
            "статический инвентарь не отдаю (fail-closed)", exc,
        )
        route_table = {}

    leadgen: dict = {}
    accounts: dict = {}
    old: dict = {}
    for city, types in STATIC_ADSETS.items():
        for adset_type, adset_id in (types or {}).items():
            if route_table.get((city, adset_type)) != default_account:
                logger.warning(
                    "discover_adsets fallback: %s/%s из config не идёт в боевой "
                    "выбор — пара маршрутизирована не в act_%s (оставляю в истории)",
                    city, adset_type, default_account,
                )
                old.setdefault(city, {}).setdefault(adset_type, []).append(adset_id)
                continue
            leadgen.setdefault(city, {})[adset_type] = adset_id
            accounts.setdefault(city, {})[adset_type] = default_account
    mql: dict = {}
    for city, adset_id in STATIC_MQL.items():
        if route_table.get((city, "MQL")) != default_account:
            logger.warning(
                "discover_adsets fallback: %s/MQL из config не идёт в боевой "
                "выбор — пара маршрутизирована не в act_%s (оставляю в истории)",
                city, default_account,
            )
            old.setdefault(city, {}).setdefault("MQL", []).append(adset_id)
            continue
        mql[city] = adset_id
        accounts.setdefault(city, {})["MQL"] = default_account
    return {
        "leadgen": leadgen,
        "mql": mql,
        "old": old,
        "accounts": accounts,
        "discovered_at": datetime.now().isoformat(),
        "source": "fallback",
    }


def _discover_single_account(account_key: str, force_refresh: bool = False) -> dict:
    """Одиночный скан кабинета активного thread-контекста (online/bot).

    Поведение прежнее (до мультикабинетного скана): кабинет и токен берутся
    из fb_token_provider по активному контексту, карта роутинга не участвует.
    """
    cached = _CACHE.get(account_key)
    if not force_refresh and cached and (time.time() - cached["ts"]) < _TTL_SECONDS:
        result = dict(cached["data"])
        result["source"] = "cache"
        return result

    try:
        from services.fb_token_provider import get_fb_account_id

        account_id = str(get_fb_account_id()).removeprefix("act_")
        all_adsets = _fetch_account_adsets(account_id)
        grouped = _group_classified(all_adsets)

        if not grouped["leadgen"]:
            logger.warning("discover_adsets: leadgen пуст — fallback на config")
            raise RuntimeError("Ни одного классифицированного leadgen адсета не найдено")

        # accounts той же вложенной формы, что у мультикабинетного скана:
        # читателям нужна одна форма, а не две (здесь кабинет всегда один).
        accounts: dict = {}
        for city, types in grouped["leadgen"].items():
            for adset_type in types:
                accounts.setdefault(city, {})[adset_type] = account_id
        for city in grouped["mql"]:
            accounts.setdefault(city, {})["MQL"] = account_id
        result = {
            **grouped,
            "accounts": accounts,
            "discovered_at": datetime.now().isoformat(),
            "source": "fb_api",
        }
        _CACHE[account_key] = {"data": dict(result), "ts": time.time()}

        if account_key != "online":
            _log_config_diff(grouped["leadgen"], grouped["mql"])

        return result

    except Exception as e:
        logger.warning("discover_adsets fallback на config: %s", e)
        if account_key == "online":
            # config.ADSETS относится к оффлайну, подсовывать его
            # онлайн-инстансу нельзя — отдаём пустую структуру.
            return {
                "leadgen": {},
                "mql": {},
                "old": {},
                "accounts": {},
                "discovered_at": datetime.now().isoformat(),
                "source": "fallback",
            }
        from config import FB_ACCOUNT_ID

        return _config_fallback(str(FB_ACCOUNT_ID).removeprefix("act_"))


def _log_config_diff(leadgen: dict, mql: dict) -> None:
    """Логирует расхождения между обнаруженными и хардкоднутыми адсетами.
    В онлайн-режиме пропускаем — config.ADSETS относится только к оффлайну.
    """
    try:
        from config import FB_MODE
        if FB_MODE == "online":
            return
    except ImportError:
        pass
    try:
        from config import ADSETS as STATIC_ADSETS, ADSETS_MQL as STATIC_MQL
        for city, types in leadgen.items():
            static = STATIC_ADSETS.get(city, {})
            for atype, aid in types.items():
                if static.get(atype) and static[atype] != aid:
                    logger.warning(
                        "Adset обновлён: %s %s — config=%s, актуальный=%s",
                        city, atype, static[atype], aid,
                    )
        for city, aid in mql.items():
            if STATIC_MQL.get(city) and STATIC_MQL[city] != aid:
                logger.warning(
                    "MQL Adset обновлён: %s — config=%s, актуальный=%s",
                    city, STATIC_MQL[city], aid,
                )
    except Exception:
        pass


def get_adsets_dict() -> dict:
    """Совместимый интерфейс с config.ADSETS — словарь {city: {L2, L1[, PRODB]}}.

    PRODB — PRODB-адсет города из cabinet_b; ключ есть не у всех
    городов, потребители обязаны брать его через .get(), а не по индексу.
    """
    return discover_adsets()["leadgen"]


def get_mql_adsets_dict() -> dict:
    """Совместимый интерфейс с config.ADSETS_MQL — словарь {city: adset_id}."""
    return discover_adsets()["mql"]


def get_adset_accounts_dict() -> dict:
    """Карта {city: {тип: account_id}} обнаруженного инвентаря.

    Имя сменилось вместе с формой (было get_city_accounts_dict → {city: account}):
    кабинет определяется парой (город, тип), и потребитель,
    оставшийся на старом имени, обязан упасть, а не получить половину правды.
    """
    return discover_adsets()["accounts"]
