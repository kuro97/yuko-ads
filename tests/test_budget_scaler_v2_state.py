"""Тесты services/budget_scaler_v2_state.py — состояние лестницы v2.

State-файл изолируется в tmp_path (monkeypatch _V2_STATE_FILE), реальный диск
не трогаем. Момент времени передаём явно через now=... (детерминизм).
"""

import json
import multiprocessing
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import services.budget_scaler_v2_state as v2

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None

_TZ = timezone(timedelta(hours=5))
_requires_fcntl = pytest.mark.skipif(_fcntl is None, reason="fcntl недоступен (не Linux/macOS)")


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Перенаправляет state-файл во временную папку."""
    monkeypatch.setattr(v2, "_V2_STATE_FILE", tmp_path / "budget_scaler_v2_state.json")


def _t(day=16, hour=13):
    return datetime(2026, 7, day, hour, 0, 0, tzinfo=_TZ)


# --- Кулдаун ---

def test_cooldown_no_state_not_active():
    """Нет истории по адсету → кулдауна нет."""
    active, reason = v2.in_adset_cooldown("a1", "raise", 24, now=_t())
    assert active is False
    assert reason is None


def test_cooldown_active_within_window():
    """Подъём был 10ч назад, кулдаун 24ч → активен."""
    v2.record_action("a1", "raise", 100.0, 110.0, now=_t(day=16, hour=3))
    active, reason = v2.in_adset_cooldown("a1", "raise", 24, now=_t(day=16, hour=13))
    assert active is True
    assert "кулдаун" in reason


def test_cooldown_expired_after_window():
    """Подъём был 25ч назад, кулдаун 24ч → не активен."""
    v2.record_action("a1", "raise", 100.0, 110.0, now=_t(day=15, hour=12))
    active, _ = v2.in_adset_cooldown("a1", "raise", 24, now=_t(day=16, hour=13))
    assert active is False


def test_cooldown_raise_and_decrease_independent():
    """Кулдаун подъёма не блокирует снижение (разные поля)."""
    v2.record_action("a1", "raise", 100.0, 110.0, now=_t(day=16, hour=12))
    active_dec, _ = v2.in_adset_cooldown("a1", "decrease", 24, now=_t(day=16, hour=13))
    assert active_dec is False


# --- record_action + baseline ---

def test_record_action_sets_fields_and_baseline():
    """record_action фиксирует время, last_set_budget и baseline=prev при первом касании."""
    v2.record_action("a1", "raise", 100.0, 115.0, now=_t())
    state = json.loads((v2._V2_STATE_FILE).read_text())
    entry = state["adsets"]["a1"]
    assert entry["last_set_budget_usd"] == 115.0
    assert entry["last_raise_at"] is not None
    assert entry["last_set_at"] is not None
    assert entry["baseline_budget_usd"] == 100.0  # опорный = бюджет ДО действия


def test_get_baseline_first_touch_fixes_current():
    """Первое обращение к baseline фиксирует current, далее не меняется."""
    assert v2.get_baseline_budget("a1", 100.0, now=_t()) == 100.0
    # Даже если текущий бюджет вырос — baseline прежний
    assert v2.get_baseline_budget("a1", 200.0, now=_t()) == 100.0


# --- Детект ручной правки ---

def test_detect_manual_edit_fresh_big_delta_true_and_rebaseline():
    """last_set свежий (<48ч) и текущий FB-бюджет отличается >5% → True + ребейзлайн."""
    v2.record_action("a1", "raise", 100.0, 100.0, now=_t(day=16, hour=10))
    # Владелец поднял до 150 (+50%)
    assert v2.detect_manual_edit("a1", 150.0, tolerance_pct=5.0, now=_t(day=16, hour=13)) is True
    # Ребейзлайн: baseline и last_set → 150
    state = json.loads((v2._V2_STATE_FILE).read_text())
    entry = state["adsets"]["a1"]
    assert entry["baseline_budget_usd"] == 150.0
    assert entry["last_set_budget_usd"] == 150.0
    # Повторный вызов уже не срабатывает (расхождения нет)
    assert v2.detect_manual_edit("a1", 150.0, tolerance_pct=5.0, now=_t(day=16, hour=14)) is False


def test_detect_manual_edit_within_tolerance_false():
    """Расхождение в пределах 5% → не считаем ручной правкой."""
    v2.record_action("a1", "raise", 100.0, 100.0, now=_t(day=16, hour=10))
    assert v2.detect_manual_edit("a1", 103.0, tolerance_pct=5.0, now=_t(day=16, hour=13)) is False


def test_detect_manual_edit_stale_false():
    """last_set старше 48ч → детект не срабатывает (окно свежести прошло)."""
    v2.record_action("a1", "raise", 100.0, 100.0, now=_t(day=13, hour=10))
    assert v2.detect_manual_edit("a1", 150.0, tolerance_pct=5.0, now=_t(day=16, hour=13)) is False


def test_detect_manual_edit_no_history_false():
    """Нет записи по адсету → False."""
    assert v2.detect_manual_edit("nope", 150.0, now=_t()) is False


# --- Hold ---

def test_hold_set_and_active():
    v2.set_hold("a1", 7, now=_t(day=16))
    assert v2.is_on_hold("a1", now=_t(day=18)) is True
    assert v2.is_on_hold("a1", now=_t(day=24)) is False  # 8 дней спустя


def test_hold_absent_false():
    assert v2.is_on_hold("a1", now=_t()) is False


# --- Undo-карта ---

def test_undo_record_get_mark_done_idempotent():
    v2.record_undo_entry("a1", prev_budget_usd=100.0, new_budget_usd=90.0, now=_t())
    entry = v2.get_undo_entry("a1")
    assert entry is not None
    assert entry["prev_budget_usd"] == 100.0
    assert entry["done"] is False

    assert v2.mark_undo_done("a1", now=_t()) is True
    assert v2.get_undo_entry("a1")["done"] is True
    # Идемпотентно — повторный вызов не падает и остаётся done
    assert v2.mark_undo_done("a1", now=_t()) is True
    assert v2.get_undo_entry("a1")["done"] is True


def test_undo_get_absent_none():
    assert v2.get_undo_entry("nope") is None
    assert v2.mark_undo_done("nope") is False


def test_prune_drops_old_undo():
    """Undo старше 30 дней вычищается prune."""
    v2.record_undo_entry("old", 100.0, 90.0, now=_t(day=1))  # 15 июля - 1 = давно? нет
    # Кладём заведомо старую запись напрямую
    state = v2._load_state()
    state["undo"]["ancient"] = {
        "adset_id": "ancient", "prev_budget_usd": 100.0, "new_budget_usd": 90.0,
        "at": (_t(day=16) - timedelta(days=40)).isoformat(), "done": False,
    }
    v2._save_state(state)
    v2.prune(now=_t(day=16))
    assert v2.get_undo_entry("ancient") is None


def test_prune_caps_undo_entries():
    """Больше 200 undo-записей → prune обрезает до 200 самых свежих."""
    state = v2._load_state()
    base = _t(day=16)
    for i in range(250):
        state["undo"][f"a{i}"] = {
            "adset_id": f"a{i}", "prev_budget_usd": 100.0, "new_budget_usd": 90.0,
            "at": (base - timedelta(minutes=i)).isoformat(), "done": False,
        }
    v2._save_state(state)
    v2.prune(now=base)
    state2 = v2._load_state()
    assert len(state2["undo"]) == v2.MAX_UNDO_ENTRIES
    # Самая свежая (a0) осталась, самая старая (a249) ушла
    assert "a0" in state2["undo"]
    assert "a249" not in state2["undo"]


# --- Устойчивость к битому файлу ---

def test_broken_file_returns_default():
    v2._V2_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    v2._V2_STATE_FILE.write_text("{ это не json", encoding="utf-8")
    state = v2._load_state()
    assert state == {"adsets": {}, "undo": {}}


# --- Битый state в mutation-пути → fail-closed (0 решений на подъём) ---

def test_corrupt_state_gates_fail_closed():
    """Битый v2-state: гейты кулдауна/hold блокируют, mutation не затирает файл."""
    v2._V2_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    v2._V2_STATE_FILE.write_text("{битый json", encoding="utf-8")

    # Кулдаун-гейт: битый state → активен (fail-closed, блок подъёма)
    active, reason = v2.in_adset_cooldown("a1", "raise", 24, now=_t())
    assert active is True
    assert reason is not None

    # Hold-гейт: битый state → считаем адсет замороженным (бот не трогает)
    assert v2.is_on_hold("a1", now=_t()) is True

    # Mutation-путь: не перезаписывает битый файл пустым дефолтом
    with pytest.raises(v2.V2StateError):
        v2.record_action("a1", "raise", 100.0, 110.0, now=_t())

    # Файл остался прежним (не затёрт)
    assert v2._V2_STATE_FILE.read_text(encoding="utf-8") == "{битый json"


def test_corrupt_state_read_helpers_safe():
    """Битый state: get_undo_entry → None, detect_manual_edit → False (без записи)."""
    v2._V2_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    v2._V2_STATE_FILE.write_text("{битый", encoding="utf-8")
    assert v2.get_undo_entry("a1") is None
    assert v2.detect_manual_edit("a1", 150.0, now=_t()) is False
    # detect не затёр файл
    assert v2._V2_STATE_FILE.read_text(encoding="utf-8") == "{битый"


# --- Baseline только из валидного finite положительного бюджета ---

@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 0.0, -10.0])
def test_baseline_rejects_invalid_current(bad):
    """Нет baseline и current невалиден → V2StateError, мусор не записан."""
    with pytest.raises(v2.V2StateError):
        v2.get_baseline_budget("a1", bad, now=_t())
    # ничего не сохранено на диск
    assert not v2._V2_STATE_FILE.exists()


def test_record_action_invalid_prev_no_baseline():
    """Невалидный prev не становится baseline; валидный new — записывается."""
    v2.record_action("a1", "raise", float("nan"), 110.0, now=_t())
    entry = v2._load_state()["adsets"]["a1"]
    assert entry["baseline_budget_usd"] is None
    assert entry["last_set_budget_usd"] == 110.0


def test_record_action_invalid_new_nullifies_last_set():
    """Невалидный new не служит опорой детекта: last_set_budget_usd = None."""
    v2.record_action("a1", "raise", 100.0, float("inf"), now=_t())
    entry = v2._load_state()["adsets"]["a1"]
    assert entry["last_set_budget_usd"] is None
    # baseline из валидного prev всё же зафиксирован
    assert entry["baseline_budget_usd"] == 100.0


# --- Prune adset-записей: возраст + потолок размера, защита активных ---

def _adset_entry(iso: str) -> dict:
    return {
        "last_raise_at": iso, "last_decrease_at": None, "last_set_at": iso,
        "last_set_budget_usd": 100.0, "baseline_budget_usd": 100.0, "hold_until": None,
    }


def test_prune_drops_aged_adset():
    """Adset без hold/undo с отметкой старше ADSET_RETENTION_DAYS → удаляется."""
    base = _t(day=16)
    state = v2._load_state()
    state["adsets"]["stale"] = _adset_entry((base - timedelta(days=100)).isoformat())
    v2._save_state(state)
    v2.prune(now=base)
    assert "stale" not in v2._load_state()["adsets"]


def test_prune_keeps_held_adset():
    """Активный hold защищает adset-запись от чистки."""
    v2.set_hold("held", 30, now=_t(day=16))
    v2.prune(now=_t(day=16))
    assert "held" in v2._load_state()["adsets"]


def test_prune_caps_adset_entries(monkeypatch):
    """Свыше потолка → prune оставляет MAX_ADSET_ENTRIES самых свежих."""
    monkeypatch.setattr(v2, "MAX_ADSET_ENTRIES", 10)
    base = _t(day=16)
    state = v2._load_state()
    # Отметки на 72ч в прошлом (старше окна ручной правки → НЕ защищены),
    # но моложе 90д → чистит только потолок размера.
    for i in range(25):
        iso = (base - timedelta(hours=72, minutes=i)).isoformat()
        state["adsets"][f"a{i}"] = _adset_entry(iso)
    v2._save_state(state)
    v2.prune(now=base)
    out = v2._load_state()["adsets"]
    assert len(out) == 10
    assert "a0" in out       # свежайшая осталась
    assert "a24" not in out  # старейшая ушла


def test_prune_keeps_adset_with_active_undo(monkeypatch):
    """Незакрытый undo защищает adset-запись даже при переполнении/возрасте."""
    monkeypatch.setattr(v2, "MAX_ADSET_ENTRIES", 5)
    base = _t(day=16)
    state = v2._load_state()
    old_iso = (base - timedelta(hours=72)).isoformat()
    state["adsets"]["keepme"] = _adset_entry(old_iso)
    state["undo"]["keepme"] = {
        "adset_id": "keepme", "prev_budget_usd": 100.0, "new_budget_usd": 90.0,
        "at": old_iso, "done": False,
    }
    for i in range(10):
        iso = (base - timedelta(hours=72, minutes=i + 1)).isoformat()
        state["adsets"][f"n{i}"] = _adset_entry(iso)
    v2._save_state(state)
    v2.prune(now=base)
    out = v2._load_state()["adsets"]
    assert "keepme" in out  # защищён активным undo


# --- Два процесса меняют РАЗНЫЕ адсеты → обе записи сохраняются (нет lost update) ---

def _mp_v2_record_worker(state_path, adset_ids, ready_evt, go_evt, result_q):
    import services.budget_scaler_v2_state as _v2
    _v2._V2_STATE_FILE = Path(state_path)
    ready_evt.set()
    go_evt.wait(timeout=10)
    try:
        for aid in adset_ids:
            _v2.record_action(aid, "raise", 100.0, 110.0)
        result_q.put(("ok", len(adset_ids)))
    except Exception as exc:  # pragma: no cover — диагностика падения воркера
        result_q.put(("err", repr(exc)))


@_requires_fcntl
def test_two_processes_different_adsets_both_persist():
    """Два процесса конкурентно пишут разные адсеты — flock не даёт потерять правки."""
    state_path = str(v2._V2_STATE_FILE)
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)

    mp = multiprocessing.get_context("spawn")
    q = mp.Queue()
    r1 = mp.Event()
    r2 = mp.Event()
    go = mp.Event()

    ids_a = [f"a{i}" for i in range(20)]
    ids_b = [f"b{i}" for i in range(20)]
    p1 = mp.Process(target=_mp_v2_record_worker, args=(state_path, ids_a, r1, go, q))
    p2 = mp.Process(target=_mp_v2_record_worker, args=(state_path, ids_b, r2, go, q))
    p1.start()
    p2.start()
    try:
        assert r1.wait(timeout=15)
        assert r2.wait(timeout=15)
        go.set()  # оба стартуют почти одновременно
        res = [q.get(timeout=25), q.get(timeout=25)]
    finally:
        p1.join(10)
        p2.join(10)

    assert all(tag == "ok" for tag, _ in res), res

    state = v2._load_state()
    for aid in ids_a + ids_b:
        assert aid in state["adsets"], f"потеряна запись {aid} (lost update)"
