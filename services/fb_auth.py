"""
Facebook OAuth — хелперы для авторизации.
Обмен code → token, получение аккаунтов, проверка токена.
"""
import logging
from urllib.parse import urlencode

import requests

from config import FB_APP_ID, FB_APP_SECRET, FB_REDIRECT_URI

logger = logging.getLogger(__name__)

FB_GRAPH = "https://graph.facebook.com/v21.0"
FB_OAUTH_DIALOG = "https://www.facebook.com/dialog/oauth"
SCOPES = "ads_management,ads_read,business_management,email"
TIMEOUT = 10


def get_auth_url(state: str) -> str:
    """Генерирует URL для редиректа на Facebook OAuth dialog."""
    params = {
        "client_id": FB_APP_ID,
        "redirect_uri": FB_REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "response_type": "code",
    }
    return f"{FB_OAUTH_DIALOG}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Обменивает authorization code на short-lived токен (1-2 часа)."""
    resp = requests.get(
        f"{FB_GRAPH}/oauth/access_token",
        params={
            "client_id": FB_APP_ID,
            "client_secret": FB_APP_SECRET,
            "redirect_uri": FB_REDIRECT_URI,
            "code": code,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise ValueError(f"FB OAuth error: {data['error'].get('message', data['error'])}")
    return data


def exchange_long_lived(short_token: str) -> dict:
    """Обменивает short-lived токен на long-lived (60 дней)."""
    resp = requests.get(
        f"{FB_GRAPH}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": FB_APP_ID,
            "client_secret": FB_APP_SECRET,
            "fb_exchange_token": short_token,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise ValueError(f"FB token exchange error: {data['error'].get('message', data['error'])}")
    return data


def get_user_info(token: str) -> dict:
    """Получает информацию о пользователе (id, name, email)."""
    resp = requests.get(
        f"{FB_GRAPH}/me",
        params={"access_token": token, "fields": "id,name,email"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_ad_accounts(token: str) -> list[dict]:
    """Список рекламных аккаунтов пользователя."""
    resp = requests.get(
        f"{FB_GRAPH}/me/adaccounts",
        params={
            "access_token": token,
            "fields": "id,name,currency,account_status",
            "limit": 100,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def debug_token(token: str) -> dict:
    """Проверяет валидность токена через FB debug_token API."""
    app_token = f"{FB_APP_ID}|{FB_APP_SECRET}"
    resp = requests.get(
        f"{FB_GRAPH}/debug_token",
        params={"input_token": token, "access_token": app_token},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("data", {})
