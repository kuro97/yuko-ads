"""
Единый провайдер токена/аккаунта Facebook.
Поддерживает несколько FB-аккаунтов (оффлайн cabinet_a + онлайн Acme + bot).
Активный аккаунт переключается через thread-local context.
"""
import logging
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Thread-local контекст текущего аккаунта.
# None / "offline" — использовать стандартный (FB_TOKEN, FB_ACCOUNT_ID).
# "online" — использовать FB_TOKEN_ONLINE, FB_ACCOUNT_ID_ONLINE.
# "bot" — использовать FB_TOKEN_BOT, FB_ACCOUNT_ID_BOT (кабинет автономных запусков бота).
# "offline:<account_id>" — маршрутизированный оффлайн-кабинет карты
#   город→кабинет (services/launch_routing.py): тот же оффлайн FB_TOKEN
#   (он имеет ads_management в каждом кабинете карты), но другой account_id
#   (например «ACME cabinet_b», куда переехала CityF 08.2026).
#   Имя контекста строится ТОЛЬКО через offline_account_context() —
#   незарегистрированный кабинет отвергается (fail-closed).
_ctx = threading.local()

# Префикс имени контекста маршрутизированного оффлайн-кабинета.
_OFFLINE_ROUTED_PREFIX = "offline:"


def set_active_account(name: str | None) -> None:
    """Устанавливает текущий FB-аккаунт для thread.
    name: None | "offline" | "online" | "bot" """
    _ctx.account = name


def get_active_account() -> str | None:
    """Возвращает имя текущего активного аккаунта (None = offline)."""
    return getattr(_ctx, "account", None)


@contextmanager
def fb_account(name: str | None):
    """Контекст-менеджер: временно переключиться на другой FB аккаунт.

        with fb_account("online"):
            launch_creative(...)  # все FB API запросы идут под online токеном
    """
    prev = get_active_account()
    set_active_account(name)
    try:
        yield
    finally:
        set_active_account(prev)


def _registered_offline_accounts() -> set[str]:
    """Code-owned реестр оффлайн-кабинетов: config.FB_ACCOUNT_ID + карта роутинга.

    Возвращает нормализованные account_id (без префикса act_). Ошибка чтения
    карты роутинга не роняет вызов: реестр сжимается до дефолтного кабинета,
    а незарегистрированный кабинет отвергает offline_account_context /
    get_fb_account_id (fail-closed ниже по стеку).
    """
    accounts: set[str] = set()
    try:
        from config import FB_ACCOUNT_ID

        default_account = str(FB_ACCOUNT_ID or "").replace("act_", "").strip()
        if default_account:
            accounts.add(default_account)
    except ImportError:
        pass
    try:
        from services.launch_routing import accounts_to_scan

        accounts.update(accounts_to_scan())
    except Exception as exc:  # noqa: BLE001 — деградация до дефолтного кабинета
        logger.warning("Карта роутинга город→кабинет недоступна: %s", exc)
    return accounts


def offline_account_context(account_id: str) -> str | None:
    """Имя thread-контекста для оффлайн-кабинета account_id (fail-closed).

    - дефолтный кабинет (config.FB_ACCOUNT_ID, cabinet_a) → None (обычный оффлайн);
    - кабинет из карты роутинга город→кабинет → "offline:<account_id>";
    - незарегистрированный кабинет → RuntimeError, никаких молчаливых дефолтов.
    """
    normalized = str(account_id or "").replace("act_", "").strip()
    if not normalized.isdigit():
        raise RuntimeError(
            f"offline_account_context: невалидный FB account_id {account_id!r}"
        )
    try:
        from config import FB_ACCOUNT_ID

        default_account = str(FB_ACCOUNT_ID or "").replace("act_", "").strip()
    except ImportError:
        default_account = ""
    if default_account and normalized == default_account:
        return None
    if normalized in _registered_offline_accounts():
        return f"{_OFFLINE_ROUTED_PREFIX}{normalized}"
    raise RuntimeError(
        f"FB кабинет {normalized} не зарегистрирован в оффлайн-реестре "
        "(config.FB_ACCOUNT_ID / services/launch_routing.py) — отказ (fail-closed)"
    )


def get_fb_token() -> str:
    """Возвращает FB API token. Учитывает активный аккаунт thread.
    Приоритет: thread context > OAuth credentials > config env."""
    account = get_active_account()

    # Маршрутизированный оффлайн-кабинет использует ОБЫЧНЫЙ оффлайн токен:
    # FB_TOKEN имеет ads_management во всех кабинетах карты роутинга.
    if isinstance(account, str) and account.startswith(_OFFLINE_ROUTED_PREFIX):
        account = None

    # Активный онлайн-аккаунт — берём отдельный токен
    if account == "online":
        from config import FB_TOKEN_ONLINE
        if FB_TOKEN_ONLINE:
            return FB_TOKEN_ONLINE
        raise RuntimeError(
            "Активен онлайн-аккаунт, но FB_TOKEN_ONLINE не задан в .env"
        )

    # Активный bot-аккаунт (автономные запуски) — берём отдельный токен
    if account == "bot":
        from config import FB_TOKEN_BOT
        if FB_TOKEN_BOT:
            return FB_TOKEN_BOT
        raise RuntimeError(
            "Активен bot-аккаунт, но FB_TOKEN_BOT не задан в .env"
        )

    # OAuth credentials (если есть — для оффлайн по умолчанию)
    try:
        from services.fb_credentials import get_active_token
        token = get_active_token()
        if token:
            return token
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"Ошибка загрузки OAuth токена: {e}")

    # Fallback на legacy env var (оффлайн)
    from config import FB_TOKEN
    if FB_TOKEN:
        return FB_TOKEN

    raise RuntimeError("FB токен не найден: нет OAuth credentials и нет FB_TOKEN в env")


def get_fb_account_id() -> str:
    """Возвращает FB account ID. Учитывает активный аккаунт thread."""
    account = get_active_account()

    # Маршрутизированный оффлайн-кабинет — id зашит в имя контекста,
    # но принимается только кабинет из code-owned реестра (fail-closed).
    if isinstance(account, str) and account.startswith(_OFFLINE_ROUTED_PREFIX):
        routed = account[len(_OFFLINE_ROUTED_PREFIX):]
        if routed.isdigit() and routed in _registered_offline_accounts():
            return routed
        raise RuntimeError(
            f"Маршрутизированный FB кабинет {routed!r} не зарегистрирован "
            "в оффлайн-реестре — отказ (fail-closed)"
        )

    # Активный онлайн-аккаунт — отдельный ID
    if account == "online":
        from config import FB_ACCOUNT_ID_ONLINE
        if FB_ACCOUNT_ID_ONLINE:
            return FB_ACCOUNT_ID_ONLINE.replace("act_", "")
        raise RuntimeError(
            "Активен онлайн-аккаунт, но FB_ACCOUNT_ID_ONLINE не задан в .env"
        )

    # Активный bot-аккаунт (автономные запуски) — отдельный ID
    if account == "bot":
        from config import FB_ACCOUNT_ID_BOT
        if FB_ACCOUNT_ID_BOT:
            return FB_ACCOUNT_ID_BOT.replace("act_", "")
        raise RuntimeError(
            "Активен bot-аккаунт, но FB_ACCOUNT_ID_BOT не задан в .env"
        )

    # OAuth credentials
    try:
        from services.fb_credentials import get_active_account_id
        account_id = get_active_account_id()
        if account_id:
            return account_id.replace("act_", "")
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"Ошибка загрузки account_id из OAuth: {e}")

    # Fallback (оффлайн)
    from config import FB_ACCOUNT_ID
    if FB_ACCOUNT_ID:
        return FB_ACCOUNT_ID

    raise RuntimeError("FB account ID не найден: нет OAuth credentials и нет FB_ACCOUNT_ID в env")
