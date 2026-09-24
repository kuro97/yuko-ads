"""A3: подтверждает, что реальная сеть в тестах заблокирована (pytest-socket),
а in-process TestClient (ASGI, без реальных сокетов) продолжает работать.

Мокаем google.genai через sys.modules ДО импорта web.app — как в test_api_auth.py,
т.к. в среде разработки пакет google-generativeai не установлен.
"""

import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Добавляем корень проекта в path
sys.path.insert(0, str(Path(__file__).parent.parent))

# --- Мокаем google.genai до импорта web.app ---
_google_mock = MagicMock()
_genai_mock = MagicMock()
_genai_types_mock = MagicMock()
sys.modules.setdefault("google", _google_mock)
sys.modules.setdefault("google.genai", _genai_mock)
sys.modules.setdefault("google.genai.types", _genai_types_mock)
_google_mock.genai = _genai_mock

import pytest
import requests
from pytest_socket import SocketBlockedError

from web.app import app  # noqa: E402


def test_socket_blocked_outside_allowed_hosts():
    """A3: реальный сетевой запрос к внешнему хосту блокируется pytest-socket
    (autouse-фикстура _no_real_network в conftest.py), а не улетает в интернет
    и не висит таймаутом."""
    with pytest.raises(SocketBlockedError):
        requests.get("https://facebook.com", timeout=5)


def test_socket_blocked_raw_socket():
    """A3: даже сырой socket.socket() к внешнему хосту заблокирован."""
    with pytest.raises(SocketBlockedError):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("facebook.com", 443))


def test_testclient_still_works():
    """A3: ASGI TestClient (httpx ASGITransport) работает in-process без реальных
    сокетов — блокировка сети его не затрагивает."""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
