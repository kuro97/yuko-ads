"""
Зашифрованное хранение FB OAuth credentials.
Fernet encryption, JSON-файл data/fb_credentials.json.
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
CREDENTIALS_FILE = DATA_DIR / "fb_credentials.json"
KEY_FILE = DATA_DIR / ".fernet_key"


def _get_fernet() -> Fernet:
    """Получает Fernet instance. Ключ: env → файл → автогенерация."""
    try:
        from config import FERNET_KEY
        if FERNET_KEY:
            return Fernet(FERNET_KEY.encode())
    except (ImportError, AttributeError):
        pass

    if KEY_FILE.exists():
        return Fernet(KEY_FILE.read_bytes().strip())

    # Автогенерация
    key = Fernet.generate_key()
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(key)
    logger.info("Сгенерирован новый Fernet ключ: data/.fernet_key")
    return Fernet(key)


def save_credentials(data: dict) -> None:
    """Шифрует access_token и сохраняет credentials."""
    fernet = _get_fernet()

    # Шифруем токен
    token = data.pop("access_token", None)
    if token:
        data["access_token_encrypted"] = fernet.encrypt(token.encode()).decode()

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    if "connected_at" not in data:
        data["connected_at"] = data["updated_at"]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_credentials() -> dict | None:
    """Загружает credentials. Возвращает None если файла нет."""
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        return json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Ошибка чтения credentials: {e}")
        return None


def delete_credentials() -> None:
    """Удаляет файл credentials."""
    if CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.unlink()
        logger.info("FB credentials удалены")


def get_active_token() -> str | None:
    """Возвращает расшифрованный токен. None если нет или истёк."""
    creds = load_credentials()
    if not creds:
        return None

    # Проверяем срок
    expires_at = creds.get("expires_at")
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= datetime.now(timezone.utc):
                logger.warning("FB OAuth токен истёк")
                return None
        except ValueError:
            pass

    encrypted = creds.get("access_token_encrypted")
    if not encrypted:
        return None

    try:
        fernet = _get_fernet()
        return fernet.decrypt(encrypted.encode()).decode()
    except Exception as e:
        logger.error(f"Ошибка расшифровки токена: {e}")
        return None


def get_active_account_id() -> str | None:
    """Возвращает account_id из credentials."""
    creds = load_credentials()
    if not creds:
        return None
    return creds.get("account_id")


def is_token_expiring_soon(days: int = 7) -> bool:
    """Проверяет, истекает ли токен в ближайшие N дней."""
    creds = load_credentials()
    if not creds:
        return False

    expires_at = creds.get("expires_at")
    if not expires_at:
        return False

    try:
        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp - datetime.now(timezone.utc) < timedelta(days=days)
    except ValueError:
        return False
