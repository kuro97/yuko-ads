"""
Общие фикстуры для тестов Yuko.
Мокают все внешние API: Facebook, Trello, Google Drive, Supabase.
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import jwt as pyjwt
import pytest
import pytest_socket
import starlette.testclient as _starlette_testclient

# Добавляем корень проекта в path
sys.path.insert(0, str(Path(__file__).parent.parent))

# --- Изоляция БД: тесты НИКОГДА не трогают data/decisions.db владельца ---
# conftest импортируется pytest ДО тест-модулей, поэтому перенаправление
# срабатывает раньше, чем web/app.py вызовет init_db() на импорте.
# Причина необходимости: миграции проверяют checksum, и любой рассинхрон
# файла миграции с уже применённой боевой БД ронял бы сбор тестов целиком.
# Временная БД создаётся с нуля, миграции применяются к ней в актуальном виде.
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="acme-tests-data-"))
_TEST_DB_PATH = _TEST_DATA_DIR / "decisions.db"
os.environ.setdefault("CREATIVE_KB_PATH", str(_TEST_DB_PATH))
os.environ.setdefault("OWNER_ACTION_DB_PATH", str(_TEST_DB_PATH))
# Фиктивный токен: код FB-клиента требует токен на импорте/вызове,
# сеть в тестах всё равно замокана (pytest_socket).
os.environ.setdefault("FB_TOKEN", "test-fb-token")

import agent.database as _agent_database  # noqa: E402  (после sys.path)

_agent_database.DATA_DIR = _TEST_DATA_DIR

# Модули с собственным жёстко прошитым путём к боевой БД — уводим туда же.
try:  # pragma: no cover — страховка, отсутствие модуля не должно ронять сбор
    import services.creative_intelligence as _creative_intelligence  # noqa: E402

    _creative_intelligence._DATA_DIR = _TEST_DATA_DIR
except Exception:  # noqa: BLE001
    pass

try:  # pragma: no cover
    import services.adset_cleaner as _adset_cleaner  # noqa: E402

    _adset_cleaner._DECISIONS_DB_PATH = _TEST_DB_PATH
except Exception:  # noqa: BLE001
    pass


# --- JWT тестовые данные ---

TEST_JWT_SECRET = "test-jwt-secret-for-testing"
TEST_USER_ID = "550e8400-e29b-41d4-a716-446655440000"
TEST_USER_EMAIL = "test@example.com"

# Обратная совместимость
TEST_API_KEY = "test-secret-key"


def _make_test_token(user_id=TEST_USER_ID, email=TEST_USER_EMAIL, exp_delta=3600):
    """Создаёт тестовый JWT токен."""
    payload = {
        "sub": user_id,
        "email": email,
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_delta,
    }
    return pyjwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")


# --- Моковые данные ---

SAMPLE_AD = {
    "id": "123456789",
    "name": "CityA | Петров / Тема А / Подтема 1",
    "status": "ACTIVE",
    "created_time": "2026-02-20T10:00:00+0000",
    "days_running": 14,
    "spend": 150.0,
    "leads": 8,
    "cpl": 18.75,
    "ctr": 1.5,
    "cpm": 5.0,
    "impressions": 30000,
    "clicks": 450,
    "qual_pct": None,
    "romi": None,
    "payments": None,
}

SAMPLE_CARD = {
    "id": "card123",
    "name": "Петров / Тема А / Подтема 1",
    "desc": "https://instagram.com/acme\nНужна бесплатная консультация?\nОставьте номер.",
    "dueComplete": False,
    "due": None,
}


@pytest.fixture
def sample_ad():
    """Объявление с типичными метриками."""
    return SAMPLE_AD.copy()


@pytest.fixture
def sample_card():
    """Карточка Trello."""
    return SAMPLE_CARD.copy()


@pytest.fixture
def mock_fb_api():
    """Мокает все запросы к Facebook API."""
    with patch("integrations.facebook.session.get") as mock_get, \
         patch("integrations.facebook.session.post") as mock_post:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": []},
        )
        mock_get.return_value.raise_for_status = MagicMock()

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"id": "new_ad_123"},
        )
        mock_post.return_value.raise_for_status = MagicMock()

        yield {"get": mock_get, "post": mock_post}


# --- Мок Supabase ---

class MockSupabaseQuery:
    """Мок для одного запроса к Supabase (chaining API)."""
    def __init__(self, storage: list):
        self._storage = storage  # общий список данных таблицы
        self._filters = {}
        self._is_delete = False
        self._inserted = None

    def select(self, *args, **kwargs):
        return self

    def insert(self, data):
        if isinstance(data, list):
            self._storage.extend(data)
            self._inserted = data
        else:
            self._storage.append(data)
            self._inserted = [data]
        return self

    def upsert(self, data):
        return self.insert(data)

    def update(self, data):
        return self

    def delete(self):
        self._is_delete = True
        return self

    def eq(self, field, value):
        self._filters[field] = value
        return self

    def ilike(self, *args):
        return self

    def gte(self, *args):
        return self

    def lte(self, *args):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args):
        return self

    def range(self, *args):
        return self

    def _match(self, row):
        """Проверяет row на соответствие фильтрам."""
        for field, value in self._filters.items():
            if row.get(field) != value:
                return False
        return True

    def execute(self):
        result = MagicMock()
        if self._inserted is not None:
            result.data = self._inserted
            result.count = len(self._inserted)
        elif self._is_delete:
            # Удаляем и возвращаем удалённые
            deleted = [r for r in self._storage if self._match(r)]
            self._storage[:] = [r for r in self._storage if not self._match(r)]
            result.data = deleted
            result.count = len(deleted)
        elif self._filters:
            matched = [r for r in self._storage if self._match(r)]
            result.data = matched
            result.count = len(matched)
        else:
            result.data = list(self._storage)
            result.count = len(self._storage)
        return result


class MockSupabaseClient:
    """Мок для Supabase Client."""
    def __init__(self):
        self.auth = MagicMock()
        self._table_data = {}  # {name: list} — хранилище данных

    def table(self, name):
        if name not in self._table_data:
            self._table_data[name] = []
        return MockSupabaseQuery(self._table_data[name])


@pytest.fixture(autouse=True)
def mock_supabase():
    """Мокает Supabase клиент для всех тестов."""
    mock_client = MockSupabaseClient()
    with patch("agent.supabase_client._client", mock_client), \
         patch("agent.supabase_client.get_supabase", return_value=mock_client):
        yield mock_client


@pytest.fixture(autouse=True)
def set_test_jwt_secret():
    """Устанавливает тестовый JWT secret и API key."""
    with patch("config.SUPABASE_JWT_SECRET", TEST_JWT_SECRET), \
         patch("config.API_KEY", TEST_API_KEY):
        yield


@pytest.fixture(autouse=True)
def _isolate_cron_failure_state(tmp_path, monkeypatch):
    """Уводит state-файл стража повторных провалов кронов (services.cron_heartbeat.
    _FAILURE_STATE_FILE) во временный каталог для КАЖДОГО теста.

    Иначе кроновые тесты (budget_scaler, adset_cleaner, guardian, autopilot_live,
    sync_mql и др.), которые прогоняют реальные крон-функции и дёргают
    report_cron_failure/report_cron_success, писали бы в реальный data/ и копили
    счётчик подряд-провалов между прогонами — на 3-м накоплении крон попытался бы
    отправить реальный критический алерт прямо во время тестов."""
    import services.cron_heartbeat as cron_hb
    monkeypatch.setattr(cron_hb, "_FAILURE_STATE_FILE", tmp_path / "cron_failure_state.json")
    yield


@pytest.fixture(autouse=True)
def _isolate_media_cache(tmp_path, monkeypatch):
    """Уводит CoW-кэш медиа во временный каталог для КАЖДОГО теста.

    Staging-тесты гоняют настоящий _copy_regular_file, а он кладёт байты в
    services.media_cache. Без изоляции прогон тестов на сервере писал бы
    мусор прямо в боевой data/media_cache."""
    import services.media_cache as media_cache
    monkeypatch.setattr(
        media_cache, "REPORT_CHECKER_MEDIA_CACHE_ROOT", tmp_path / "media_cache"
    )
    # Проба reflink кэшируется на процесс: сбрасываем, чтобы новый корень проверился.
    monkeypatch.setattr(media_cache, "_reflink_supported", None)
    yield


@pytest.fixture(autouse=True)
def _isolate_overview_route_cache():
    """Чистит in-memory кэши overview-роутера (web.overview_routes._cache и
    _trends_cache) до и после КАЖДОГО теста.

    Зачем: GET /api/overview кэширует ответ на 25 минут в module-level dict.
    Загрязнитель — tests/test_api_security.py::test_heavy_refresh_rate_limited_provider_not_called:
    он делает POST /api/overview/refresh?days=14 с замоканным get_overview →
    пишет в _cache[14] фейковое {"ok": True}. Этот dict — глобал модуля, он
    переживает `with patch(...)` и весь тест, и позже отдаётся в
    tests/test_overview.py::TestOverviewAPI::test_api_overview_success
    (GET /api/overview?days=14) вместо реального расчёта → order-dependent провал
    (200, но тело {"ok": True} без "cities"). Сброс кэша гарантирует чистый старт.

    Только очищаем, если модуль уже импортирован (не форсим импорт в тестах,
    которые web-роуты вообще не трогают — иначе можно нарушить порядок мока
    google.genai в auth-тестах)."""
    def _clear() -> None:
        mod = sys.modules.get("web.overview_routes")
        if mod is not None:
            getattr(mod, "_cache", {}).clear()
            getattr(mod, "_trends_cache", {}).clear()

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _isolate_launch_state():
    """Сбрасывает глобальное состояние запуска web.app (launch_status и
    _launch_preflight_running) до и после КАЖДОГО теста.

    Зачем: POST /api/launch ставит launch_status["running"]=True, а обратно его
    сбрасывает фоновый _run_checked_web_launch (finally). Тест, который мокает
    раннер (напр. tests/test_api.py::test_launch_calls_service), оставляет
    глобал с running=True навсегда — и любой последующий тест, дёргающий
    /api/launch (tests/test_api_security.py::test_launch_trello_error_hides_secrets),
    получает 409 LAUNCH_IN_PROGRESS вместо своего сценария → order-dependent
    провал (виден в выборке -k "...launch...", где оба теста попадают в прогон).
    Сбрасываем к идл-форме web.app._launch_status_idle() — единый источник
    формы вместо копий по тест-файлам. clear()+update() сохраняют идентичность
    dict — на него держат ссылки эндпоинты и фоновые потоки.

    Только сбрасываем, если модуль уже импортирован (не форсим импорт web.app
    в тестах, которые его не трогают — та же логика, что в
    _isolate_overview_route_cache)."""
    def _reset() -> None:
        mod = sys.modules.get("web.app")
        if mod is None:
            return
        with mod._launch_lock:
            mod.launch_status.clear()
            mod.launch_status.update(mod._launch_status_idle())
            mod._launch_preflight_running = False

    _reset()
    yield
    _reset()


@pytest.fixture
def auth_headers():
    """Заголовки авторизации с тестовым JWT."""
    token = _make_test_token()
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def test_token():
    """Тестовый JWT токен."""
    return _make_test_token()


# --- A3: блокировка реальной сети во всех тестах ---

@pytest.fixture(autouse=True)
def _no_real_network():
    """A3: блокируем реальные сокеты во ВСЕХ тестах. ASGI TestClient (httpx
    ASGITransport) работает in-process без сокетов — он не затронут. Любой
    незамоканный вызов requests/urllib к внешнему хосту → SocketBlockedError,
    а не таймаут в facebook.com (сейчас это ест 11 минут прогона).

    allow_unix_socket=True — иначе disable_socket() блокирует и AF_UNIX-сокеты
    (socketpair), которые asyncio event loop создаёт для self-pipe (Starlette/
    TestClient используют asyncio под капотом) — без этого падает ~100 тестов
    с SocketBlockedError на пустом месте, никак не связанным с реальной сетью."""
    pytest_socket.disable_socket(allow_unix_socket=True)
    yield
    pytest_socket.enable_socket()


# --- A2: автоматический X-API-Key во все мутирующие запросы TestClient ---

@pytest.fixture(autouse=True)
def _inject_api_key_header(request):
    """A2: во ВСЕ мутирующие запросы TestClient добавляем X-API-Key=TEST_API_KEY,
    если заголовок не задан явно. Так 80+ тест-файлов не правим руками — middleware
    _require_api_key (403 на POST/PUT/DELETE/PATCH без ключа) их больше не режет.

    Пропускаем файлы, которые НАМЕРЕННО проверяют отказ без ключа/с неверным ключом —
    они сами задают заголовки и ожидают 403/503."""
    nodeid = request.node.nodeid
    # Эти файлы сами управляют X-API-Key (проверяют 403/503/happy) — не вмешиваемся.
    _auth_managed_files = ("test_api_auth", "test_api_key_fail_closed", "test_cleanup_stream_auth")
    # Плюс — любой отдельный тест в ДРУГИХ файлах, который сам проверяет отказ
    # без ключа/с неверным ключом (test_..._no_key_returns_403 и т.п.). Таких тестов
    # больше, чем 3 auth-файла (напр. tests/test_brain_routes.py, tests/test_match_outcomes.py) —
    # ищем по имени теста, а не перечисляем файлы вручную.
    _auth_managed_test_markers = ("no_key", "wrong_key", "missing_key", "no_api_key")
    if any(marker in nodeid for marker in _auth_managed_files) or \
            any(marker in nodeid for marker in _auth_managed_test_markers):
        yield
        return

    _orig_request = _starlette_testclient.TestClient.request

    def _patched_request(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        # setdefault: если тест уже задал X-API-Key (в т.ч. неверный) — не трогаем
        headers.setdefault("X-API-Key", TEST_API_KEY)
        kwargs["headers"] = headers
        return _orig_request(self, method, url, **kwargs)

    with patch.object(_starlette_testclient.TestClient, "request", _patched_request):
        yield
