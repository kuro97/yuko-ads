import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

load_dotenv()

# Facebook — оффлайн рекламный кабинет cabinet_a (основной)
FB_TOKEN = os.getenv("FB_TOKEN")
FB_ACCOUNT_ID = os.getenv("FB_ACCOUNT_ID", "152882611033373")

# Facebook — онлайн рекламный кабинет Acme (отдельный BM).
# Используется при campaign_type=mql_online через fb_account("online") контекст.
FB_TOKEN_ONLINE = os.getenv("FB_TOKEN_ONLINE", "")
FB_ACCOUNT_ID_ONLINE = os.getenv("FB_ACCOUNT_ID_ONLINE", "334355943837505")

# Facebook — кабинет автономных запусков yuko bot (собственный BM Acme).
# Используется через fb_account("bot") контекст.
FB_TOKEN_BOT = os.getenv("FB_TOKEN_BOT", "")
FB_ACCOUNT_ID_BOT = os.getenv("FB_ACCOUNT_ID_BOT", "")

# Режим работы основного FB кабинета:
#   offline — оффлайн-точки компании (N городов × L2/L1 + MQL website по городам)
#   online  — онлайн-кабинет (один псевдогород "Онлайн", классификация по destination_type)
FB_MODE = os.getenv("FB_MODE", "offline").lower()

# Facebook CAPI (Conversions API для CRM событий)
FB_DATASET_ID = os.getenv("FB_DATASET_ID", "")       # ID датасета Meta CAPI
FB_CAPI_TOKEN = os.getenv("FB_CAPI_TOKEN", "")        # System User Token для CAPI
AMO_FB_LEAD_ID_FIELD = os.getenv("AMO_FB_LEAD_ID_FIELD", "FB Lead ID")  # Имя поля в AMO CRM

# Facebook OAuth
FB_APP_ID = os.getenv("FB_APP_ID")
FB_APP_SECRET = os.getenv("FB_APP_SECRET")
FB_REDIRECT_URI = os.getenv("FB_REDIRECT_URI", "http://localhost:8000/auth/facebook/callback")
FERNET_KEY = os.getenv("FERNET_KEY")
FB_PAGE_ID = os.getenv("FB_PAGE_ID", "739512142241302")
FB_CAMPAIGN_ID = os.getenv("FB_CAMPAIGN_ID", "6001745347491")

# Адсеты по городам — ОФЛАЙН-ФОЛБЭК, а не истина.
# Истина — живой каталог кабинета: agent.adset_discovery (и напрямую
# services.coverage_guard.FacebookAdsetDirectory для стража покрытия).
# Адсеты в кабинете пересоздают, и строки ниже могут устареть молча (часть из них
# указывает на запаузенные адсеты). Код, который читает карту напрямую, должен
# это учитывать; рантайм берёт живой каталог.
ADSETS = {
    "CityA":  {"L2": "56893093790545", "L1": "58029559394919"},
    "CityB":  {"L2": "6442045225120", "L1": "6051270040345"},  # PAUSED в кабинете
    "CityC": {"L2": "6968438338330", "L1": "6802728523188"},  # PAUSED в кабинете
    "CityD":  {"L2": "6053000174625", "L1": "6192133011072"},  # PAUSED в кабинете
    "CityE":  {"L2": "6935132592425", "L1": "6424411652232"},  # PAUSED в кабинете
}

# Дополнительный ADV+ адсет CityA (PAUSED — не используется)
ADSETS_EXTRA = {
    "CityA ADV+": {"L1": "6522188420405"},
}

# MQL-адсеты (Instagram | OFFSITE_CONVERSIONS | MQL-CAPI)
# Кампания: "Owner | WEBSITE | MQL | v1" — трафик на сайт www.example.com
# Использовать когда нужно гнать трафик на лендинг (а не в FB Lead Gen форму).
ADSETS_MQL = {
    "CityA":  "6401591464375",
    "CityB":  "6115144594328",
    "CityC": "6472025201061",
    "CityD":  "6065062464526",
    "CityE":  "6639257501075",
}

# UTM-короткие коды городов для website-кампании
CITY_UTM_CODES = {
    "CityA": "cta",
    "CityB": "ctb",
    "CityC": "ctc",
    "CityD": "ctd",
    "CityE": "cte",
}

# URL сайта для website-кампании
WEBSITE_LANDING_URL = "https://www.example.com/"

# Старые адсеты (для аналитики исторических данных)
ADSETS_OLD = {
    "CityA":  {"L2": "6279946932993", "L1": "6671551048110"},
    "CityB":  {"L2": "6442045225120", "L1": "6051270040345"},
    "CityC": {"L2": "6408074948442", "L1": "6330804058573"},
    "CityD":  {"L2": "6053000174625", "L1": "6192133011072"},
    "CityE":  {"L2": "6935132592425", "L1": "6424411652232"},
}

# Лид-формы
LEAD_FORMS = {
    "L2": {"form_id": "953081145432003",  "cta": "LEARN_MORE"},
    "L1": {"form_id": "2168647923341625", "cta": "GET_QUOTE"},
}

# Языки объявлений: L1 — основной язык (по умолчанию), L2 — второй язык аудитории.
# Маркеры L2 в имени Trello-карточки (токены через запятую, регистр не важен):
# «Тема А / l2» → адсет L2. Явный тег «[L2]» в имени или описании работает всегда.
L2_MARKERS: frozenset[str] = frozenset(
    m.strip().lower()
    for m in os.getenv("L2_MARKERS", "l2").split(",")
    if m.strip()
) or frozenset({"l2"})
# Названия языков для промптов генераторов текстов («Язык текста: <название>»).
L1_LANGUAGE_NAME = os.getenv("L1_LANGUAGE_NAME", "русский")
L2_LANGUAGE_NAME = os.getenv("L2_LANGUAGE_NAME", "английский")

AD_TITLE = "ACME"

# Фиксированные тексты объявлений (message / body) — шаблон, замените своими.
# L1 — основной язык, L2 — заглушка на английском: подставьте текст на втором языке.
AD_BODY = {
    "L2": "[L2] Want to know how Product A can help you? Leave your number below — we will call you back with a free consultation.",
    "L1": "Хотите узнать, как «Продукт A» поможет именно вам? Оставьте номер по кнопке ниже — перезвоним и проведём бесплатную консультацию.",
}

# Тексты для кампаний второй продуктовой линии PRODB («Продукт B»).
# Используются при campaign_type=leadgen_prodb — отдельная кнопка в дашборде.
AD_BODY_PRODB = {
    "L2": "[L2] Interested in Product B? Leave your number below — we will call you back with a free consultation.",
    "L1": "Интересует «Продукт B»? Оставьте номер по кнопке ниже — перезвоним и проведём бесплатную консультацию.",
}

# Instagram User ID для PRODB-объявлений (acme_prodb_ig).
# Узнать ID можно в Ads Manager → создай объявление → выбери Instagram acme_prodb_ig →
# Inspect Element или в URL после выбора. Длинное число 17841...
# Пока не задан — fallback на основной аккаунт (11651524554970514).
FB_IG_ACME_PRODB = os.getenv("FB_IG_ACME_PRODB", "")

# Адсеты онлайн-кампании PRODB "Owner / PRODB online / v1" (онлайн-кабинет
# 334355943837505). Используются при campaign_type=prodb_online — отдельная кнопка.
# Адсеты называются просто "L2"/"L1", автодискавери их не ловит — поэтому по ID.
# page/IG/Lead-форма наследуются из шаблона существующего объявления адсета.
PRODB_ONLINE_ADSETS = {
    "L2": os.getenv("PRODB_ONLINE_ADSET_L2", "120301232117300217"),
    "L1": os.getenv("PRODB_ONLINE_ADSET_L1", "120005540508341541"),
}

# Instagram, доступный ОНЛАЙН-кабинету (334355943837505) — acme_online_ig.
# Шаблоны онлайн-адсетов используют acme_prodb_ig (17174201582453886), к которому
# у онлайн-кабинета нет доступа (ошибка 1815199) — поэтому при создании объявлений
# подменяем IG на этот. Когда acme_prodb_ig привяжут к кабинету в Business Manager —
# можно поставить сюда 17174201582453886.
FB_IG_ONLINE = os.getenv("FB_IG_ONLINE", "11000953224235643")

# Trello
TRELLO_API_KEY = os.getenv("TRELLO_API_KEY")
TRELLO_TOKEN = os.getenv("TRELLO_TOKEN")
TRELLO_BOARD_ID = os.getenv("TRELLO_BOARD_ID", "AbCdEf12")
TRELLO_DONE_LIST_NAME = "Готово"

# AMO CRM
AMO_DOMAIN = os.getenv("AMO_DOMAIN")  # {domain}.amocrm.ru
AMO_CLIENT_ID = os.getenv("AMO_CLIENT_ID")
AMO_CLIENT_SECRET = os.getenv("AMO_CLIENT_SECRET")
AMO_REFRESH_TOKEN = os.getenv("AMO_REFRESH_TOKEN")
AMO_PIPELINE_ID = int(os.getenv("AMO_PIPELINE_ID", "3480844"))  # Воронка "Новые продажи"
AMO_QUAL_STATUS_ID = int(os.getenv("AMO_QUAL_STATUS_ID", "0"))  # ID этапа "Квалифицирован"
AMO_PAYMENT_STATUS_IDS = [
    int(x) for x in os.getenv("AMO_PAYMENT_STATUS_IDS", "").split(",") if x.strip()
]  # ID этапов оплаты

# ID этапов встреч воронки "Новые продажи" (3480844) — для критерия удержания (ARCH-hold-meetings).
# Сверяется запросом GET /api/v4/leads/pipelines/{pipeline_id} → _embedded.statuses.
# «ВСТРЕЧА НАЗНАЧЕНА» (31239894) + «Встреча ПОДТВЕРЖДЕНА» (34482950) — назначена (ещё не прошла):
# этап "подтверждена" стоит МЕЖДУ "назначена" и "состоялась" по sort, факт встречи ещё впереди.
MEETING_SCHEDULED_STATUS_IDS: set[int] = {
    int(x) for x in os.getenv("MEETING_SCHEDULED_STATUS_IDS", "31239894,34482950").split(",") if x.strip()
}
# «ВСТРЕЧА СОСТОЯЛАСЬ» (44446055) — встреча прошла.
MEETING_HELD_STATUS_IDS: set[int] = {
    int(x) for x in os.getenv("MEETING_HELD_STATUS_IDS", "44446055").split(",") if x.strip()
}

# AMO Auto-Source webhook
AMO_WEBHOOK_SECRET: str = os.getenv("AMO_WEBHOOK_SECRET", "").strip()

# Whitelist user_id операторов отдела продаж (CSV в env)
_operator_ids_raw = os.getenv("AMO_OPERATOR_USER_IDS", "")
AMO_OPERATOR_USER_IDS: set[int] = {
    int(x.strip()) for x in _operator_ids_raw.split(",")
    if x.strip().isdigit()
}

# Custom field ID для source_id (может отличаться) — необязательное
AMO_SOURCE_ID_FIELD: int | None = (
    int(os.getenv("AMO_SOURCE_ID_FIELD"))
    if os.getenv("AMO_SOURCE_ID_FIELD", "").isdigit()
    else None
)

# Публичный URL — для webhook (только для документации/деплоя)
PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").strip()

# Курс валюты (для ROMI: расход в USD, выручка в LCY)
USD_TO_LCY = int(os.getenv("USD_TO_LCY", "100"))

# Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = "claude-opus-4-6"
CLAUDE_HAIKU_MODEL = os.getenv("CLAUDE_HAIKU_MODEL", "claude-haiku-4-5")
# Ad Generator V2 — модель для генерации рекламных текстов
CLAUDE_SONNET_MODEL = os.getenv("CLAUDE_SONNET_MODEL", "claude-sonnet-5")

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Бот для здоровья системы (сторож/пульс); если не задан — шлём через основной
TELEGRAM_HEALTH_BOT_TOKEN = os.getenv("TELEGRAM_HEALTH_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_OWNER_USER_ID = os.getenv("TELEGRAM_OWNER_USER_ID")
APPROVAL_CALLBACK_SECRET = os.getenv("APPROVAL_CALLBACK_SECRET")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")

# Персональное одобрение нельзя отключить через окружение.
OWNER_APPROVAL_REQUIRED = True


class OwnerApprovalConfigError(RuntimeError):
    """Настройки персонального одобрения отсутствуют или небезопасны."""


@dataclass(frozen=True, slots=True)
class OwnerApprovalConfig:
    bot_token: str
    chat_id: int
    owner_user_id: int
    callback_secret: str
    webhook_secret: str
    db_path: Path


_OWNER_SECRET_PLACEHOLDERS = (
    "change-me",
    "changeme",
    "placeholder",
    "replace-me",
    "your_",
    "your-",
    "<",
    ">",
)


def _owner_integer(
    environment: Mapping[str, str],
    name: str,
    *,
    positive: bool,
) -> int:
    source_value = environment.get(name, "")
    if not isinstance(source_value, str):
        raise OwnerApprovalConfigError(f"{name} должен быть строкой с integer")
    raw_value = source_value.strip()
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise OwnerApprovalConfigError(f"{name} должен быть integer") from exc
    if (positive and parsed <= 0) or (not positive and parsed == 0):
        requirement = "положительным" if positive else "ненулевым"
        raise OwnerApprovalConfigError(f"{name} должен быть {requirement} integer")
    return parsed


def _owner_secret(
    environment: Mapping[str, str],
    name: str,
) -> str:
    source_value = environment.get(name, "")
    if not isinstance(source_value, str):
        raise OwnerApprovalConfigError(f"{name} должен быть строкой")
    value = source_value.strip()
    lowered = value.casefold()
    if not value or any(marker in lowered for marker in _OWNER_SECRET_PLACEHOLDERS):
        raise OwnerApprovalConfigError(f"{name} должен быть задан реальным секретом")
    if len(value.encode("utf-8")) < 32 or len(set(value)) < 8:
        raise OwnerApprovalConfigError(
            f"{name} должен содержать минимум 32 байта неповторяющегося секрета"
        )
    return value


def load_owner_approval_config(
    environment: Mapping[str, str] | None = None,
) -> OwnerApprovalConfig:
    """Валидирует consent-настройки непосредственно перед запуском worker."""

    source = os.environ if environment is None else environment
    bot_token_value = source.get("TELEGRAM_BOT_TOKEN", "")
    if not isinstance(bot_token_value, str):
        raise OwnerApprovalConfigError("TELEGRAM_BOT_TOKEN должен быть строкой")
    bot_token = bot_token_value.strip()
    if not bot_token or any(
        marker in bot_token.casefold() for marker in _OWNER_SECRET_PLACEHOLDERS
    ):
        raise OwnerApprovalConfigError("TELEGRAM_BOT_TOKEN должен быть настроен")
    owner_user_id = _owner_integer(
        source,
        "TELEGRAM_OWNER_USER_ID",
        positive=True,
    )
    chat_id = _owner_integer(source, "TELEGRAM_CHAT_ID", positive=False)
    callback_secret = _owner_secret(source, "APPROVAL_CALLBACK_SECRET")
    webhook_secret = _owner_secret(source, "TELEGRAM_WEBHOOK_SECRET")
    if webhook_secret in {callback_secret, bot_token}:
        raise OwnerApprovalConfigError(
            "TELEGRAM_WEBHOOK_SECRET должен отличаться от callback secret и bot token"
        )
    if callback_secret == bot_token:
        raise OwnerApprovalConfigError(
            "APPROVAL_CALLBACK_SECRET должен отличаться от bot token"
        )
    db_path_value = source.get(
        "OWNER_ACTION_DB_PATH",
        source.get("CREATIVE_KB_PATH", "data/decisions.db"),
    )
    if not isinstance(db_path_value, str) or not db_path_value.strip():
        raise OwnerApprovalConfigError("OWNER_ACTION_DB_PATH должен быть задан")
    db_path = Path(db_path_value).expanduser()
    return OwnerApprovalConfig(
        bot_token=bot_token,
        chat_id=chat_id,
        owner_user_id=owner_user_id,
        callback_secret=callback_secret,
        webhook_secret=webhook_secret,
        db_path=db_path,
    )


def validate_owner_approval_config(
    settings: OwnerApprovalConfig,
) -> OwnerApprovalConfig:
    """Не позволяет тестовому DI случайно обойти startup-валидацию."""

    return load_owner_approval_config(
        {
            "TELEGRAM_BOT_TOKEN": settings.bot_token,
            "TELEGRAM_CHAT_ID": str(settings.chat_id),
            "TELEGRAM_OWNER_USER_ID": str(settings.owner_user_id),
            "APPROVAL_CALLBACK_SECRET": settings.callback_secret,
            "TELEGRAM_WEBHOOK_SECRET": settings.webhook_secret,
            "OWNER_ACTION_DB_PATH": str(settings.db_path),
        }
    )


# Creative Intelligence KB (тот же файл что и основная БД)
CREATIVE_KB_PATH = os.getenv("CREATIVE_KB_PATH", "data/decisions.db")

# Gemini Vision (для visual/text/customer скоринга)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Ad Generator V2 — модель Gemini для генерации рекламных текстов
GEMINI_AD_MODEL = os.getenv("GEMINI_AD_MODEL", "gemini-2.0-flash")

# API-аутентификация. Без дефолта: если не задан в env — fail-closed (см. web/app.py middleware).
API_KEY = os.getenv("API_KEY")

# Веб-сервер
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))

# Stripe (биллинг)
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_START = os.getenv("STRIPE_PRICE_START")
STRIPE_PRICE_GROWTH = os.getenv("STRIPE_PRICE_GROWTH")
STRIPE_PRICE_AGENCY = os.getenv("STRIPE_PRICE_AGENCY")

# Админы
ADMIN_EMAILS = [e.strip() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]


# Approval Checker. Неверный env никогда не ослабляет обязательную проверку.
def _checker_bool(name: str, default: bool = True) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return True


def _checker_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


_PROJECT_ROOT = Path(__file__).resolve().parent
REPORT_CHECKER_DATA_ROOT = Path(
    os.getenv("REPORT_CHECKER_DATA_ROOT", str(_PROJECT_ROOT / "data"))
).expanduser().resolve()

REPORT_CHECKER_ENABLED = _checker_bool("REPORT_CHECKER_ENABLED", True)
REPORT_CHECKER_ENFORCE_REPORTS = _checker_bool("REPORT_CHECKER_ENFORCE_REPORTS", True)
REPORT_CHECKER_ENFORCE_ACTIONS = _checker_bool("REPORT_CHECKER_ENFORCE_ACTIONS", True)

# Permit живёт ровно столько, сколько допускает §9: launch 300s, остальные actions 120s.
REPORT_CHECKER_LAUNCH_TTL_SECONDS = _checker_int(
    "REPORT_CHECKER_LAUNCH_TTL_SECONDS", 300, 1, 300
)
REPORT_CHECKER_PAUSE_TTL_SECONDS = _checker_int(
    "REPORT_CHECKER_PAUSE_TTL_SECONDS", 120, 1, 120
)
REPORT_CHECKER_UNPAUSE_TTL_SECONDS = _checker_int(
    "REPORT_CHECKER_UNPAUSE_TTL_SECONDS", 120, 1, 120
)
REPORT_CHECKER_SCALE_TTL_SECONDS = _checker_int(
    "REPORT_CHECKER_SCALE_TTL_SECONDS", 120, 1, 120
)

# Закрытые лимиты contracts/source adapters.
REPORT_CHECKER_MAX_BATCH_ACTIONS = 20
REPORT_CHECKER_MAX_LAUNCH_DESTINATIONS = 10
REPORT_CHECKER_FB_OBJECT_CHUNK_SIZE = 50
REPORT_CHECKER_LOCAL_DB_MAX_AGE_SECONDS = 300
REPORT_CHECKER_CREATIVE_KB_MAX_AGE_SECONDS = 3 * 60 * 60
REPORT_CHECKER_JSON_MAX_AGE_SECONDS = 30
REPORT_CHECKER_RUNTIME_MAX_AGE_SECONDS = 5
REPORT_CHECKER_DAILY_INVENTORY_MAX_AGE_SECONDS = 15 * 60
REPORT_CHECKER_DAILY_MANIFEST_MAX_AGE_SECONDS = 36 * 60 * 60
REPORT_CHECKER_DAILY_MANIFEST_RETENTION_DAYS = 32
REPORT_CHECKER_PENDING_BRIEF_RETENTION_DAYS = 14
REPORT_CHECKER_NOTIFICATION_BUFFER_CAPACITY = 500
REPORT_CHECKER_MAX_JSON_BYTES = _checker_int(
    "REPORT_CHECKER_MAX_JSON_BYTES", 1024 * 1024, 1024, 10 * 1024 * 1024
)

# Runtime paths перечислены явно: adapter не строит путь из внешнего имени source.
REPORT_CHECKER_STAGING_ROOT = REPORT_CHECKER_DATA_ROOT / "approval_staging"
# Общий CoW-кэш медиа: staging получает reflink вместо своей копии байт.
REPORT_CHECKER_MEDIA_CACHE_ROOT = REPORT_CHECKER_DATA_ROOT / "media_cache"
REPORT_CHECKER_MEDIA_CACHE_ENABLED = _checker_bool(
    "REPORT_CHECKER_MEDIA_CACHE_ENABLED", True
)
# Кэш-файл живёт столько дней с последнего обращения.
REPORT_CHECKER_MEDIA_CACHE_TTL_DAYS = _checker_int(
    "REPORT_CHECKER_MEDIA_CACHE_TTL_DAYS", 30, 1, 365
)
REPORT_CHECKER_AUDIT_PATH = REPORT_CHECKER_DATA_ROOT / "approval_checker_audit.jsonl"
REPORT_CHECKER_OPERATION_LOCK_PATH = REPORT_CHECKER_DATA_ROOT / "approval_checker.lock"
REPORT_CHECKER_RECONCILIATION_PATH = (
    REPORT_CHECKER_DATA_ROOT / "approval_checker_reconciliation.json"
)
REPORT_CHECKER_METRICS_MANIFEST_PATH = REPORT_CHECKER_DATA_ROOT / "metrics_snapshot_state.json"
REPORT_CHECKER_DECISIONS_DB_PATH = REPORT_CHECKER_DATA_ROOT / "decisions.db"
REPORT_CHECKER_PENDING_BRIEFS_PATH = REPORT_CHECKER_DATA_ROOT / "pending_briefs.json"
REPORT_CHECKER_SETTINGS_PATH = REPORT_CHECKER_DATA_ROOT / "settings.json"
REPORT_CHECKER_CRON_HEARTBEATS_PATH = REPORT_CHECKER_DATA_ROOT / "cron_heartbeats.json"
REPORT_CHECKER_CRON_FAILURE_PATH = REPORT_CHECKER_DATA_ROOT / "cron_failure_state.json"
REPORT_CHECKER_MONITOR_STATE_PATHS = {
    "GUARDIAN_STATE": REPORT_CHECKER_DATA_ROOT / "guardian_state.json",
    "BRIEF_GENERATOR_STATE": REPORT_CHECKER_DATA_ROOT / "brief_gen_state.json",
    "ANOMALY_ALERT_STATE": REPORT_CHECKER_DATA_ROOT / "anomaly_alerts_state.json",
    "EXPIRED_OFFER_STATE": REPORT_CHECKER_DATA_ROOT / "expired_offer_guard_state.json",
    "COVERAGE_STATE": REPORT_CHECKER_DATA_ROOT / "coverage_state.json",
    "ADS_WATCHDOG_STATE": REPORT_CHECKER_DATA_ROOT / "ads_watchdog_state.json",
    "ADSET_SPEND_GUARD_STATE": REPORT_CHECKER_DATA_ROOT / "adset_spend_guard_state.json",
    "CDP_SPEND_ALERT_STATE": REPORT_CHECKER_DATA_ROOT / "cdp_spend_alerts_state.json",
}

REPORT_CHECKER_STAGING_DIR_MODE = 0o700
REPORT_CHECKER_STAGING_FILE_MODE = 0o600
