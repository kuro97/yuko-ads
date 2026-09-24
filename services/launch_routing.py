"""Маршрутизация (город, тип адсета) → рекламный кабинет FB для конвейера запуска.

Единственный источник истины «какой инвентарь льётся в какой кабинет».

История гранулярности:
- сначала карта была городской (город → кабинет): CityF переехала
  из cabinet_a (config.FB_ACCOUNT_ID) в «ACME cabinet_b» (act_29716040622546856,
  тот же FB_TOKEN видит оба), остальные города остались в cabinet_a;
- затем гранулярность поднята до пары (город, тип): L2-адсеты расщеплённых городов
  (CityA, CityB, CityC, CityE, CityD) мигрировали в cabinet_b, а их L1 и
  MQL остались в cabinet_a. Городской карты стало недостаточно: при ней конвейер
  клал новые L2-креативы в СПЯЩИЕ (PAUSED) L2-адсеты cabinet_a — бюджет не тратится,
  креатив не выходит, отказа нет. Спящие дубли в cabinet_a не удаляют, это
  постоянный фон; отсекает их роутинг-фильтр discovery, а не статус адсета.
- позже в карту добавлен тип PRODB: PRODB-адсеты «Owner | PRODB | MQL |
  SO <Город> | ver1» живут в cabinet_b. До этого PRODB-карточки (campaign_type
  leadgen_prodb) резолвились по языку в обычные PRODA-адсеты L2/L1, а сами
  PRODB-адсеты discovery выбрасывал в историю (пары (город, PRODB) в карте не было).
- затем запуски переведены только в cabinet_b. L1 расщеплённых городов переведены в
  cabinet_b через settings, последняя пара cabinet_a —
  MQL (website) расщеплённых городов — выключена там же (``null``). Кабинет без единой
  пары выпадает из карты, но НЕ из наблюдения: метрики, расход, автопаузы,
  когорты, сверка галочек и разметка истории discovery считают кабинеты через
  accounts_to_scan(), а в cabinet_a ещё живут объявления с расходом. Для этого
  есть слой «наблюдать, но не запускать» — observe_accounts (ниже).

Тип адсета — тот же словарь, что возвращает agent.adset_discovery._classify_adset:
L2 / L1 (Lead Gen по языку), MQL (website-кампании) и PRODB (PRODB-адсеты; язык
карточки для PRODB влияет только на лид-форму и текст, не на выбор адсета).

Принципы (fail-closed):
- пара без записи в карте = LaunchRoutingError, а не молчаливый cabinet_a;
- неоднозначность структурно невозможна: карта — dict, одна пара → один кабинет;
- дубли одного города в двух кабинетах режет discovery-фильтр
  (agent/adset_discovery.py), который берёт пару только из её кабинета.

Переопределение без деплоя — через settings.json (тот же файл, что читает
автопилот, см. agent/scheduler.load_settings). Два слоя, применяются по порядку:

    {"launch_routing": {
        "cities": {                          # слой 1: город целиком (все типы)
            "CityF": "152882611033373",  # вернуть город в cabinet_a
            "CityG": "act_123456789",        # добавить новый город
            "CityE": null                   # исключить город из запуска
        },
        "routes": {                          # слой 2: точечно по типу
            "CityA": {"L2": "152882611033373"},  # откатить миграцию L2 CityA
            "CityD": {"MQL": null}               # исключить один тип
        },
        "observe_accounts": ["152882611033373"]  # наблюдать, но не запускать
    }}

Блок "observe_accounts" — кабинеты, которые входят в accounts_to_scan() даже
когда ни одна пара карты в них не ведёт: их видят метрики, расход, автопаузы,
когорты, сверка галочек и discovery (только как историю — запуск в них
невозможен, resolve_account по-прежнему отказывает). Ключа нет — по умолчанию
дефолтный оффлайн-кабинет (cabinet_a, config.FB_ACCOUNT_ID); ``null`` или ``[]``
выключают слой целиком. Запуск в кабинет из этого блока не открывается никогда:
маршрут задаёт только пара (город, тип).

Блок "cities" — прежний формат прежних настроек. Кабинет в нём применяется ко
ВСЕМ языковым типам города (L2 / L1 / MQL), но НЕ к PRODB: запись «вернуть город
в cabinet_a» не должна молча утащить туда и PRODB-адсет, которого в cabinet_a нет.
null же исключает город из запуска ЦЕЛИКОМ, включая PRODB: «все города» для любой
оффлайн-кампании берутся из all_cities(), и город, оставшийся в карте одной
парой PRODB, ронял бы PRODA-запуск на «все города» отказом CITY_ACCOUNT_UNROUTED
вместо пропуска. Точечно PRODB управляется через "routes" (например,
{"CityA": {"PRODB": null}} — выключить только PRODB-запуск города, оставив PRODA).
Блок "routes" точечный и применяется поверх "cities".

Невалидные записи (пустой город, неизвестный тип, не-цифровой account_id)
игнорируются с warning — карта остаётся дефолтной, отказ по немаршрутизированной
паре обеспечивает resolve_account.
"""

import hashlib
import json
import logging

logger = logging.getLogger(__name__)


class LaunchRoutingError(Exception):
    """Пара (город, тип) не может быть однозначно маршрутизирована в кабинет."""


# Кабинет «ACME cabinet_b» — сюда переехала CityF
# и L2-адсеты расщеплённых городов.
ACCOUNT_CABINET_B = "29716040622546856"

# Типы адсетов карты — словарь _classify_adset: Lead Gen по языку + website + PRODB.
ADSET_TYPES: tuple[str, ...] = ("L2", "L1", "MQL", "PRODB")

# Типы, которые кабинет из слоя «город целиком» (settings → launch_routing
# .cities) ПЕРЕНОСИТ. PRODB не входит намеренно: у переноса смысл «вернуть/увезти
# PRODA-инвентарь города», а PRODB-адсет живёт только в cabinet_b и точечно
# управляется через routes. null в том же слое выключает все ADSET_TYPES.
_CITY_LAYER_TYPES: tuple[str, ...] = ("L2", "L1", "MQL")

# Города, чьи L2-адсеты мигрировали в cabinet_b (L1 и MQL — cabinet_a).
_SPLIT_CITIES: tuple[str, ...] = ("CityA", "CityB", "CityC", "CityD", "CityE")

# Города с PRODB-адсетом «Owner | PRODB | MQL | SO <Город> | ver1» в cabinet_b:
# расщеплённые города плюс CityF.
_PRODB_CITIES: tuple[str, ...] = _SPLIT_CITIES + ("CityF",)

# Блок settings.json с переопределениями карты.
_SETTINGS_BLOCK = "launch_routing"

# Ключ слоя «наблюдать, но не запускать» внутри блока (см. docstring модуля).
_OBSERVE_KEY = "observe_accounts"


def _default_offline_account() -> str:
    """Дефолтный оффлайн-кабинет (cabinet_a) из config, без префикса act_."""
    from config import FB_ACCOUNT_ID

    return str(FB_ACCOUNT_ID).removeprefix("act_")


def _default_route_table() -> dict[tuple[str, str], str]:
    """Дефолтная карта (город, тип) → account_id (до переопределений settings).

    Порядок вставки городов держит порядок all_cities() — на него опираются
    «все города» в запуске и тесты.
    """
    cabinet_a = _default_offline_account()
    table: dict[tuple[str, str], str] = {}
    for city in _SPLIT_CITIES:
        # L2 уехал в cabinet_b, L1 и MQL остались в cabinet_a.
        table[(city, "L2")] = ACCOUNT_CABINET_B
        table[(city, "L1")] = cabinet_a
        table[(city, "MQL")] = cabinet_a
    # CityF переехала целиком; website-кампаний там нет,
    # поэтому пары MQL нет — её запуск откажет fail-closed, а не уйдёт в cabinet_a.
    table[("CityF", "L2")] = ACCOUNT_CABINET_B
    table[("CityF", "L1")] = ACCOUNT_CABINET_B
    # PRODB-адсеты всех городов карты живут в cabinet_b: PRODB-карточки
    # (leadgen_prodb) льются в них, а не в PRODA-адсеты по языку.
    for city in _PRODB_CITIES:
        table[(city, "PRODB")] = ACCOUNT_CABINET_B
    return table


def _normalize_account(value: object) -> str | None:
    """Нормализует account_id из settings: строка цифр без префикса act_.

    Возвращает None для невалидного значения — вызывающий код игнорирует
    такую запись с warning (карта не меняется, fail-closed ниже по стеку).
    """
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    normalized = value.strip().removeprefix("act_")
    if not normalized or not normalized.isdigit():
        return None
    return normalized


def _settings_block() -> dict:
    """Читает блок launch_routing из settings.json (как автопилот)."""
    try:
        from agent.scheduler import load_settings

        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 — битые settings не роняют запуск
        logger.warning("launch_routing: не удалось прочитать settings: %s", exc)
        return {}
    block = settings.get(_SETTINGS_BLOCK)
    return block if isinstance(block, dict) else {}


def _apply_city_overrides(table: dict[tuple[str, str], str], raw: object) -> None:
    """Слой 1: город целиком.

    Кабинет применяется ко всем языковым типам (L2 / L1 / MQL), PRODB при
    переносе не трогаем: PRODB-адсет живёт только в cabinet_b, и «вернуть город
    в cabinet_a» не должно молча увезти его в кабинет, где его нет. null же
    исключает город целиком, включая PRODB: список «все города» строится из
    all_cities(), и город, оставшийся в карте одной парой PRODB, ронял бы
    PRODA-запуск отказом по немаршрутизированной паре вместо пропуска города.
    Выключить только PRODB — точечно через routes {город: {"PRODB": null}}.
    """
    if not isinstance(raw, dict):
        return
    for raw_city, raw_account in raw.items():
        city = str(raw_city or "").strip()
        if not city:
            logger.warning("launch_routing: пустое имя города в settings — игнорирую")
            continue
        if raw_account is None:
            # null = осознанное исключение города из запуска (без деплоя) —
            # целиком, включая PRODB (см. докстринг).
            for adset_type in ADSET_TYPES:
                table.pop((city, adset_type), None)
            continue
        account = _normalize_account(raw_account)
        if account is None:
            logger.warning(
                "launch_routing: невалидный account_id %r для города «%s» в settings — игнорирую",
                raw_account,
                city,
            )
            continue
        for adset_type in _CITY_LAYER_TYPES:
            table[(city, adset_type)] = account


def _apply_route_overrides(table: dict[tuple[str, str], str], raw: object) -> None:
    """Слой 2: точечно {город: {тип: account|null}} — поверх слоя 1."""
    if not isinstance(raw, dict):
        return
    for raw_city, raw_types in raw.items():
        city = str(raw_city or "").strip()
        if not city:
            logger.warning("launch_routing: пустое имя города в routes — игнорирую")
            continue
        if not isinstance(raw_types, dict):
            logger.warning(
                "launch_routing: routes[«%s»] не словарь типов — игнорирую", city
            )
            continue
        for raw_type, raw_account in raw_types.items():
            adset_type = str(raw_type or "").strip().upper()
            if adset_type not in ADSET_TYPES:
                logger.warning(
                    "launch_routing: неизвестный тип адсета %r у города «%s» — игнорирую",
                    raw_type,
                    city,
                )
                continue
            if raw_account is None:
                table.pop((city, adset_type), None)
                continue
            account = _normalize_account(raw_account)
            if account is None:
                logger.warning(
                    "launch_routing: невалидный account_id %r для «%s»/%s — игнорирую",
                    raw_account,
                    city,
                    adset_type,
                )
                continue
            table[(city, adset_type)] = account


def get_route_table() -> dict[tuple[str, str], str]:
    """Актуальная карта (город, тип) → account_id: дефолт + settings."""
    table = _default_route_table()
    block = _settings_block()
    _apply_city_overrides(table, block.get("cities"))
    _apply_route_overrides(table, block.get("routes"))
    return table


def route_account(city: str, adset_type: str) -> str | None:
    """Кабинет пары или None, если пара не маршрутизирована (без исключения).

    Для discovery-фильтра, которому нужен ответ «чей это инвентарь», а не отказ.
    """
    key = (str(city or "").strip(), str(adset_type or "").strip().upper())
    return get_route_table().get(key)


def resolve_account(city: str, adset_type: str) -> str:
    """Кабинет пары (город, тип). Немаршрутизированная пара = отказ (fail-closed).

    Второй аргумент обязателен намеренно: после расщепления по типам кабинет
    города без типа не определён, и call-site не имеет права промолчать.

    Raises:
        LaunchRoutingError: город вне карты либо тип у города не маршрутизирован.
    """
    normalized_city = str(city or "").strip()
    normalized_type = str(adset_type or "").strip().upper()
    table = get_route_table()
    account = table.get((normalized_city, normalized_type))
    if account:
        return account
    if any(known_city == normalized_city for known_city, _ in table):
        raise LaunchRoutingError(
            f"Тип адсета «{normalized_type or adset_type}» города "
            f"«{normalized_city or city}» не маршрутизирован ни в один кабинет — "
            "запуск запрещён (fail-closed). Добавьте пару в "
            "services/launch_routing.py или в settings.json → launch_routing.routes."
        )
    raise LaunchRoutingError(
        f"Город «{normalized_city or city}» не маршрутизирован ни в один кабинет — "
        "запуск запрещён (fail-closed). Добавьте город в services/launch_routing.py "
        "или в settings.json → launch_routing.cities."
    )


def route_type(campaign_type: str, adset_type: str) -> str:
    """Тип маршрута для кампании: website → MQL, PRODB → PRODB, leadgen — по языку.

    Язык карточки (adset_type = L2/L1) и тип адсета совпадают только для
    lead gen. Для website-кампании карточка тоже бывает на L2, но адсеты
    там MQL — брать язык напрямую значит спросить карту не про тот инвентарь.
    Для PRODB (leadgen_prodb) инвентарь — PRODB-адсеты cabinet_b независимо
    от языка: раньше PRODB-карточки по языку уезжали в PRODA-адсеты. Язык
    при этом обязан быть валидным: он по-прежнему выбирает лид-форму.

    Raises:
        ValueError: онлайн-кампании (mql_online / prodb_online) идут в отдельный
            онлайн-кабинет и карту оффлайн-роутинга не используют.
    """
    normalized = str(campaign_type or "").strip()
    if normalized in {"mql_online", "prodb_online"}:
        raise ValueError(
            f"campaign_type={normalized!r} — онлайн-контур, карта роутинга не применяется"
        )
    if normalized == "website":
        return "MQL"
    normalized_type = str(adset_type or "").strip().upper()
    if normalized_type not in {"L2", "L1"}:
        raise ValueError(f"Неизвестный язык адсета {adset_type!r} для {normalized!r}")
    if normalized == "leadgen_prodb":
        return "PRODB"
    return normalized_type


def is_managed_city(city: str) -> bool:
    """Город присутствует в карте хотя бы одним типом.

    Нужен discovery-фильтру: у управляемого города немаршрутизированный тип
    обязан отсеиваться, а не проваливаться в ветку псевдогородов.
    """
    normalized = str(city or "").strip()
    return any(known_city == normalized for known_city, _ in get_route_table())


def all_cities() -> list[str]:
    """Список городов «все города» = уникальные города карты в порядке карты."""
    return list(dict.fromkeys(city for city, _ in get_route_table()))


def known_cities() -> list[str]:
    """Города, которые карта знает вообще: дефолт плюс города из
    переопределений settings — включая выключенные через null.

    Нужен разбору городских меток Trello (services/auto_launch.py): метка
    «CityF» остаётся городской и когда город выключен из запуска — иначе
    карточка с выключенным городом молча ушла бы во «все города».
    """
    cities = [city for city, _ in _default_route_table()]
    block = _settings_block()
    for raw in (block.get("cities"), block.get("routes")):
        if isinstance(raw, dict):
            cities.extend(str(raw_city or "").strip() for raw_city in raw)
    return list(dict.fromkeys(city for city in cities if city))


def observe_accounts() -> tuple[str, ...]:
    """Кабинеты «наблюдать, но не запускать» (settings → launch_routing
    .observe_accounts).

    Ключа нет — дефолтный оффлайн-кабинет (cabinet_a): сейчас в него не
    ведёт ни одна пара карты, а объявления с расходом там ещё живут, и без
    этого слоя он молча выпал бы из метрик, автопауз и сверки галочек.
    ``null`` / ``[]`` — слой выключен. Невалидные записи (не список, не-цифровой
    account_id) игнорируются с warning: не-список оставляет дефолт, мусорный
    элемент пропускается.
    """
    block = _settings_block()
    if _OBSERVE_KEY not in block:
        return (_default_offline_account(),)
    raw = block.get(_OBSERVE_KEY)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        logger.warning(
            "launch_routing: %s не список — оставляю дефолт (%s)",
            _OBSERVE_KEY,
            _default_offline_account(),
        )
        return (_default_offline_account(),)
    accounts: list[str] = []
    for raw_account in raw:
        account = _normalize_account(raw_account)
        if account is None:
            logger.warning(
                "launch_routing: невалидный account_id %r в %s — игнорирую",
                raw_account,
                _OBSERVE_KEY,
            )
            continue
        accounts.append(account)
    return tuple(dict.fromkeys(accounts))


def accounts_to_scan() -> tuple[str, ...]:
    """Уникальные кабинеты для скана: кабинеты карты плюс observe_accounts.

    Это scope discovery, метрик, расхода, автопауз, когорт и сверки галочек.
    Кабинет из observe_accounts попадает сюда даже без единой пары в карте —
    наблюдение не равно праву на запуск (его даёт только resolve_account).

    Дефолтный кабинет (cabinet_a) идёт первым, если он есть в scope: порядок скана
    детерминирован и не зависит от того, какой тип какого города оказался в
    таблице первым.
    """
    accounts = list(
        dict.fromkeys([*get_route_table().values(), *observe_accounts()])
    )
    default_account = _default_offline_account()
    if default_account in accounts:
        accounts.remove(default_account)
        accounts.insert(0, default_account)
    return tuple(accounts)


def cities_for_account(account_id: str) -> tuple[str, ...]:
    """Города, у которых ХОТЯ БЫ ОДИН тип маршрутизирован в данный кабинет.

    После расщепления по типам город может присутствовать в обоих кабинетах
    (L2 в cabinet_b, L1/MQL в cabinet_a) — функция отвечает «город здесь есть»,
    а не «город принадлежит кабинету».
    """
    normalized = str(account_id or "").strip().removeprefix("act_")
    return tuple(
        dict.fromkeys(
            city
            for (city, _adset_type), account in get_route_table().items()
            if account == normalized
        )
    )


def types_for_city(city: str) -> dict[str, str]:
    """Маршруты города: {тип: account_id} (пустой словарь для чужого города)."""
    normalized = str(city or "").strip()
    return {
        adset_type: account
        for (known_city, adset_type), account in get_route_table().items()
        if known_city == normalized
    }


def routing_fingerprint() -> str:
    """sha256 актуальной карты — отпечаток для кеша discovery и подписи конфига.

    Кеш discovery живёт 30 минут; без отпечатка в ключе правка settings
    вступала бы в силу с задержкой до получаса. Тот же отпечаток входит в
    config_version_sha256 запуска: манифест, застейдженный по старой карте,
    перестаёт сходиться после её смены (LAUNCH_STAGING_DRIFT) вместо тихого
    исполнения в кабинет, из которого инвентарь уже уехал.
    """
    table = get_route_table()
    canonical = sorted(
        (city, adset_type, account)
        for (city, adset_type), account in table.items()
    )
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
