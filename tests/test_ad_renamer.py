"""
Тесты T7: services/ad_renamer.py — переименователь активных объявлений.

Проверяем:
1. plan_renames строит план только для ACTIVE+PAUSED, DELETED пропущен,
   уже-с-тегом (актуальный продукт) пропущен.
2. rename_active_ads(mode='dry_run') — FB.rename_ad НЕ вызывается.
3. rename_active_ads(mode='active') и rollback — hard deny до сети.
5. format_rename_dry_run — чистая функция форматирования (без FB).

Мокаем ТОЛЬКО границу FB (integrations.facebook.rename_ad) и creative_kb
(временная sqlite через ci.init_kb(tmp_path)), см. docs/specs/ARCH-product-tags.md §9.
"""

import sqlite3
from unittest.mock import MagicMock

import pytest

from services import ad_renamer
from services import creative_intelligence as ci
from integrations.facebook_ads_mutation_transport import ForbiddenMutation


# ---------------------------------------------------------------------------
# Фикстуры (по образцу tests/test_product_report.py + tests/test_ads_watchdog.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_kb_path():
    """Сбрасываем глобальный DB_PATH creative_intelligence перед и после теста."""
    ci.DB_PATH = None
    yield
    ci.DB_PATH = None


@pytest.fixture
def kb(tmp_path):
    """Инициализирует пустую KB (полная схема через миграции) во временной директории."""
    db_path = str(tmp_path / "test.db")
    ci.init_kb(db_path)
    return db_path


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Перенаправляет data/ad_rename_state.json на tmp_path — реальный файл не трогаем."""
    state_path = tmp_path / "ad_rename_state.json"
    monkeypatch.setattr(ad_renamer, "_STATE_FILE", state_path)
    return state_path


@pytest.fixture(autouse=True)
def fast_throttle(monkeypatch):
    """Убираем реальные паузы между батчами — тесты не должны спать секундами."""
    monkeypatch.setattr(ad_renamer, "_BATCH_SLEEP_SEC", 0)


def _insert_row(db_path: str, ad_id: str, ad_name: str,
                 target_product: str | None = None, status: str = "ACTIVE",
                 effective_status: str = "", synced_days_ago: int | None = None) -> None:
    """Вставляет строку creative_kb напрямую (без бизнес-логики синка).

    effective_status — вычисляемый FB-статус ('' по умолчанию, как схема).
    synced_days_ago — если задан, synced_at = datetime('now', '-N days')
    (эмуляция протухшего/свежего синка теми же часами SQLite, что и предикат;
    None → схемный DEFAULT datetime('now') = свежий).
    """
    conn = sqlite3.connect(db_path)
    try:
        if synced_days_ago is None:
            conn.execute(
                """
                INSERT INTO creative_kb (ad_id, ad_name, target_product, status, effective_status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ad_id, ad_name, target_product, status, effective_status),
            )
        else:
            conn.execute(
                """
                INSERT INTO creative_kb
                    (ad_id, ad_name, target_product, status, effective_status, synced_at)
                VALUES (?, ?, ?, ?, ?, datetime('now', ?))
                """,
                (ad_id, ad_name, target_product, status, effective_status,
                 f"-{int(synced_days_ago)} days"),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# plan_renames
# ---------------------------------------------------------------------------


def test_plan_renames_строит_план_для_active_без_тега(kb):
    """ACTIVE объявление без тега попадает в план с корректным продуктом и new_name."""
    _insert_row(kb, "ad-1", "CityA | Тема А / prodb", target_product=None, status="ACTIVE")

    plan = ad_renamer.plan_renames()

    assert len(plan) == 1
    item = plan[0]
    assert item["ad_id"] == "ad-1"
    assert item["old_name"] == "CityA | Тема А / prodb"
    assert item["new_name"] == "CityA | Тема А / prodb [PRODB]"
    assert item["product"] == "PRODB"
    assert item["status"] == "ACTIVE"


def test_plan_renames_paused_тоже_попадает(kb):
    """PAUSED объявление без тега тоже попадает в план (не только ACTIVE)."""
    _insert_row(kb, "ad-2", "CityB | Заявка на PRODA", target_product=None, status="PAUSED")

    plan = ad_renamer.plan_renames()

    assert len(plan) == 1
    assert plan[0]["status"] == "PAUSED"
    assert plan[0]["product"] == "PRODA"


def test_plan_renames_deleted_пропущен(kb):
    """DELETED объявление НЕ попадает в план (архив не трогаем)."""
    _insert_row(kb, "ad-3", "CityC | тема", target_product="PRODB", status="DELETED")

    plan = ad_renamer.plan_renames()

    assert plan == []


def test_plan_renames_архивное_пропущено(kb):
    """ARCHIVED объявление тоже не в _RENAMABLE_STATUSES → пропущено."""
    _insert_row(kb, "ad-4", "CityD | тема", target_product="PRODA", status="ARCHIVED")

    plan = ad_renamer.plan_renames()

    assert plan == []


def test_plan_renames_уже_с_актуальным_тегом_пропущен(kb):
    """Имя уже содержит тег актуального продукта — format_ad_name идемпотентен,
    new_name == old_name → в план не попадает."""
    _insert_row(kb, "ad-5", "CityG | тема [PRODA]", target_product="PRODA", status="ACTIVE")

    plan = ad_renamer.plan_renames()

    assert plan == []


def test_plan_renames_смена_тега_на_другой_продукт_попадает(kb):
    """Имя с УСТАРЕВШИМ тегом (продукт сменился) — попадает в план, тег меняется."""
    _insert_row(kb, "ad-6", "CityH | тема [PRODB]", target_product="PRODA", status="ACTIVE")

    plan = ad_renamer.plan_renames()

    assert len(plan) == 1
    assert plan[0]["new_name"] == "CityH | тема [PRODA]"


# ---------------------------------------------------------------------------
# plan_renames: свежестный гейт (живой+свежий скоуп) — суть фикса
# ---------------------------------------------------------------------------


def test_plan_renames_протухшая_active_не_попадает(kb):
    """status='ACTIVE', но синк протух (30 дней) и effective_status не 'ACTIVE'
    (реклама по факту не крутится) — исторический хвост, в план НЕ попадает."""
    _insert_row(kb, "ad-stale", "CityA | тема", target_product=None,
                status="ACTIVE", effective_status="CAMPAIGN_PAUSED", synced_days_ago=30)

    plan = ad_renamer.plan_renames()

    assert plan == []


def test_plan_renames_effective_active_попадает_даже_со_старым_синком(kb):
    """effective_status='ACTIVE' (реклама крутится сейчас) — попадает в план,
    даже если status пуст/устарел и synced_at очень старый (свежести не требует)."""
    _insert_row(kb, "ad-live", "CityB | тема", target_product=None,
                status="", effective_status="ACTIVE", synced_days_ago=90)

    plan = ad_renamer.plan_renames()

    assert len(plan) == 1
    assert plan[0]["ad_id"] == "ad-live"
    assert plan[0]["new_name"] == "CityB | тема [ОБЩАЯ]"


def test_plan_renames_свежая_paused_попадает(kb):
    """status='PAUSED' со свежим синком (2 дня) — недавняя пауза, видна в
    кабинете, владелец хочет её переименовать → попадает в план."""
    _insert_row(kb, "ad-fresh-paused", "CityC | тема", target_product=None,
                status="PAUSED", effective_status="ADSET_PAUSED", synced_days_ago=2)

    plan = ad_renamer.plan_renames()

    assert len(plan) == 1
    assert plan[0]["ad_id"] == "ad-fresh-paused"
    assert plan[0]["status"] == "PAUSED"


def test_plan_renames_fresh_days_переопределяется(kb):
    """Протухшая (30 дней) ACTIVE-строка: при дефолте fresh_days=14 — мимо,
    при сознательном расширении fresh_days=45 — попадает (окно шире)."""
    _insert_row(kb, "ad-30d", "CityG | тема", target_product=None,
                status="ACTIVE", effective_status="CAMPAIGN_PAUSED", synced_days_ago=30)

    assert ad_renamer.plan_renames() == []                       # дефолт 14 дней
    assert ad_renamer.plan_renames(fresh_days=5) == []           # уже 5 дней
    wide = ad_renamer.plan_renames(fresh_days=45)                # расширили до 45
    assert len(wide) == 1
    assert wide[0]["ad_id"] == "ad-30d"


# ---------------------------------------------------------------------------
# rename_active_ads: dry_run
# ---------------------------------------------------------------------------


def test_dry_run_не_зовёт_fb(kb, monkeypatch):
    """dry_run (дефолт) строит план, но НЕ вызывает FB.rename_ad."""
    _insert_row(kb, "ad-1", "CityA | тема", target_product=None, status="ACTIVE")

    called = []
    monkeypatch.setattr(
        "integrations.facebook.rename_ad",
        lambda ad_id, new_name: called.append((ad_id, new_name)) or True,
        raising=False,
    )

    result = ad_renamer.rename_active_ads(mode="dry_run")

    assert called == []
    assert result["mode"] == "dry_run"
    assert result["planned"] == 1
    assert result["renamed"] == []
    assert result["failed"] == []
    assert result["error"] is None


def test_dry_run_дефолтный_режим_без_аргумента(kb, monkeypatch):
    """rename_active_ads() без аргументов = dry_run по умолчанию."""
    _insert_row(kb, "ad-1", "CityA | тема", target_product=None, status="ACTIVE")
    monkeypatch.setattr("integrations.facebook.rename_ad", lambda *a: True, raising=False)

    result = ad_renamer.rename_active_ads()

    assert result["mode"] == "dry_run"


def test_dry_run_пустой_план(kb, monkeypatch):
    """Пустая KB → planned=0, ничего не падает."""
    monkeypatch.setattr("integrations.facebook.rename_ad", lambda *a: True, raising=False)

    result = ad_renamer.rename_active_ads(mode="dry_run")

    assert result == {
        "mode": "dry_run", "planned": 0, "renamed": [], "failed": [], "error": None,
        "telemetry": {"total_in_kb": 0, "excluded_stale": 0, "planned": 0},
    }


def test_dry_run_telemetry_считает_total_excluded_planned(kb, monkeypatch):
    """telemetry: всего в KB / отсеяно как протухший хвост / в плане.

    Раскладка: A — свежая ACTIVE без тега (в плане); B — протухшая PAUSED
    (30 дней, effective не 'ACTIVE') — отсеяна свежестным гейтом; C — DELETED
    (в старый широкий фильтр не входила → excluded_stale её не считает)."""
    monkeypatch.setattr("integrations.facebook.rename_ad", lambda *a: True, raising=False)
    _insert_row(kb, "ad-A", "CityA | тема", target_product=None,
                status="ACTIVE", effective_status="ACTIVE", synced_days_ago=1)
    _insert_row(kb, "ad-B", "CityB | тема", target_product=None,
                status="PAUSED", effective_status="CAMPAIGN_PAUSED", synced_days_ago=30)
    _insert_row(kb, "ad-C", "CityC | тема", target_product=None,
                status="DELETED", effective_status="DELETED", synced_days_ago=1)

    result = ad_renamer.rename_active_ads(mode="dry_run")

    assert result["planned"] == 1
    assert result["telemetry"] == {"total_in_kb": 3, "excluded_stale": 1, "planned": 1}


# ---------------------------------------------------------------------------
# rename_active_ads: active/rollback запрещены
# ---------------------------------------------------------------------------


def test_active_запрещён_до_планирования_и_сети(kb, monkeypatch):
    _insert_row(kb, "ad-1", "CityA | тема", target_product=None, status="ACTIVE")
    network = MagicMock()
    monkeypatch.setattr("integrations.facebook.session", network, raising=False)
    with pytest.raises(ForbiddenMutation, match="RENAME_OPERATION_FORBIDDEN"):
        ad_renamer.rename_active_ads(mode="active", batch_size=1)
    network.post.assert_not_called()


def test_rollback_запрещён_даже_без_mapping(monkeypatch):
    network = MagicMock()
    monkeypatch.setattr("integrations.facebook.session", network, raising=False)
    with pytest.raises(ForbiddenMutation, match="RENAME_ROLLBACK_OPERATION_FORBIDDEN"):
        ad_renamer.rollback_renames()
    network.post.assert_not_called()


# ---------------------------------------------------------------------------
# format_rename_dry_run — чистая функция, без FB
# ---------------------------------------------------------------------------


def test_format_rename_dry_run_пустой_план():
    text = ad_renamer.format_rename_dry_run([])

    assert isinstance(text, str)
    assert "переименовывать нечего" in text


def test_format_rename_dry_run_с_данными():
    plan = [
        {"ad_id": "1", "old_name": "CityA | тема", "new_name": "CityA | тема [PRODB]", "product": "PRODB"},
        {"ad_id": "2", "old_name": "CityB | тема", "new_name": "CityB | тема [PRODA]", "product": "PRODA"},
    ]

    text = ad_renamer.format_rename_dry_run(plan)

    assert "CityA | тема → CityA | тема [PRODB]" in text
    assert "план: 2" in text
    assert len(text) <= 4096


def test_format_rename_dry_run_экранирует_html():
    """HTML-спецсимволы в именах экранируются (не ломают Telegram HTML-разметку)."""
    plan = [{"ad_id": "1", "old_name": "Тест <script>", "new_name": "Тест <script> [ОБЩАЯ]", "product": "ОБЩАЯ"}]

    text = ad_renamer.format_rename_dry_run(plan)

    assert "<script>" not in text
    assert "&lt;script&gt;" in text


# ---------------------------------------------------------------------------
# integrations.facebook.rename_ad — hard deny без сети
# ---------------------------------------------------------------------------


def test_rename_ad_запрещён_до_сети(monkeypatch):
    from integrations import facebook

    network = MagicMock()
    monkeypatch.setattr(facebook, "session", network, raising=False)
    with pytest.raises(ForbiddenMutation, match="RENAME_OPERATION_FORBIDDEN"):
        facebook.rename_ad("123", "Новое имя")
    network.post.assert_not_called()
