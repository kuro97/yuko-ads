#!/usr/bin/env python3
"""Одноразовый скрипт для получения Google Ads refresh_token через OAuth2.

Запускается один раз на машине разработчика. Открывает браузер для авторизации,
выводит refresh_token который нужно добавить в .env файл.

Требования перед запуском:
1. В GCP создан OAuth2 Desktop App (тип "Desktop application")
2. Проект: acme-ads-api
3. Scope: https://www.googleapis.com/auth/adwords
4. Скачан credentials.json ИЛИ заданы GOOGLE_ADS_CLIENT_ID и GOOGLE_ADS_CLIENT_SECRET в env

Запуск:
    python scripts/setup_google_ads_oauth.py

После получения refresh_token добавить в .env:
    GOOGLE_ADS_DEVELOPER_TOKEN=...
    GOOGLE_ADS_CLIENT_ID=...
    GOOGLE_ADS_CLIENT_SECRET=...
    GOOGLE_ADS_REFRESH_TOKEN=<полученный токен>
"""

import os
import sys

# Добавляем корень проекта в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Scope для Google Ads API
_SCOPES = ["https://www.googleapis.com/auth/adwords"]

# Redirect URI для Desktop App
_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"


def main():
    """Запускает OAuth2 flow и выводит refresh_token."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Ошибка: установите зависимости:")
        print("  pip install google-auth-oauthlib")
        sys.exit(1)

    # Читаем CLIENT_ID и CLIENT_SECRET из env или спрашиваем вручную
    client_id = os.getenv("GOOGLE_ADS_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_ADS_CLIENT_SECRET", "").strip()

    if not client_id:
        print("GOOGLE_ADS_CLIENT_ID не задан в env.")
        client_id = input("Введите Client ID: ").strip()

    if not client_secret:
        print("GOOGLE_ADS_CLIENT_SECRET не задан в env.")
        client_secret = input("Введите Client Secret: ").strip()

    if not client_id or not client_secret:
        print("Ошибка: CLIENT_ID и CLIENT_SECRET обязательны.")
        sys.exit(1)

    # Строим client config для OAuth2 Desktop App
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": [_REDIRECT_URI, "http://localhost"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }

    print("\n--- Google Ads OAuth2 Setup ---")
    print(f"Client ID: {client_id[:20]}...")
    print(f"Scope: {_SCOPES[0]}")
    print()

    # Создаём flow из конфига (без файла credentials.json)
    flow = InstalledAppFlow.from_client_config(client_config, scopes=_SCOPES)

    # run_local_server открывает браузер и слушает на localhost
    # Если браузер недоступен — используем console flow
    print("Открываю браузер для авторизации...")
    print("Если браузер не открылся — скопируйте URL вручную.\n")

    try:
        # Пробуем через локальный сервер (обычный случай)
        credentials = flow.run_local_server(
            port=0,
            prompt="consent",
            access_type="offline",
        )
    except Exception:
        # Fallback: консольный режим (для headless серверов)
        print("Браузер недоступен, используем консольный режим.")
        flow.redirect_uri = _REDIRECT_URI
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )
        print(f"\nОткройте URL в браузере:\n{auth_url}\n")
        auth_code = input("Вставьте код авторизации из браузера: ").strip()
        flow.fetch_token(code=auth_code)
        credentials = flow.credentials

    refresh_token = credentials.refresh_token

    if not refresh_token:
        print("\nОшибка: refresh_token не получен.")
        print("Убедитесь что приложение запрошено с access_type='offline' и prompt='consent'.")
        sys.exit(1)

    print("\n=== УСПЕШНО ===")
    print(f"\nRefresh Token:\n{refresh_token}")
    print("\nДобавьте в .env файл:")
    print("---")
    print(f"GOOGLE_ADS_CLIENT_ID={client_id}")
    print(f"GOOGLE_ADS_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_ADS_REFRESH_TOKEN={refresh_token}")
    print("---")
    print("\nДля работы также нужен GOOGLE_ADS_DEVELOPER_TOKEN из:")
    print("Google Ads → Инструменты → API-центр → Токен разработчика")


if __name__ == "__main__":
    main()
