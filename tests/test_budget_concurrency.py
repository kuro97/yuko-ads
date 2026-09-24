"""
Тесты конкурентного контура Budget Scaler (Wave 1B):

1B.1 — один активный active-run:
- два потока одновременно → ровно один mutation-path;
- два процесса (flock) → ровно один держатель замка;
- старый timed-out worker жив → второй tick не зовёт set_adset_budget;
- ошибка захвата замка → fail-closed skip, 0 FB.

1B.2 — reservation → provider-call → commit (services/budget_daily_cap):
- FB-успех → committed; явный отказ → release; timeout/unknown → pending и блок повтора;
- два процесса резервируют один adset → pending+committed ≤ cap;
- reconciliation success/unchanged/ambiguous раздельно;
- смена дня CityA; битый state / ошибка save / невалидные значения → 0 FB.

Внешние API замоканы, сеть заблокирована pytest-socket. Cap-state изолирован в tmp.
Модуль НЕ импортирует services.budget_scaler на верхнем уровне: spawn-дети
переимпортируют этот модуль, тяжёлый scaler им не нужен (импортируем внутри тестов).
"""

import multiprocessing
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Мокаем google до импортов проекта (как в остальных budget-тестах)
if "google.genai" not in sys.modules:
    sys.modules["google.genai"] = MagicMock()
if "google" not in sys.modules:
    sys.modules["google"] = MagicMock()

from services import budget_daily_cap

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None

_TZ = timezone(timedelta(hours=5))
_requires_fcntl = pytest.mark.skipif(_fcntl is None, reason="fcntl недоступен (не Linux/macOS)")


def _now(date_str: str = "2026-07-02", hour: int = 10) -> datetime:
    return datetime.fromisoformat(f"{date_str}T{hour:02d}:00:00").replace(tzinfo=_TZ)


@pytest.fixture(autouse=True)
def isolated_cap(tmp_path, monkeypatch):
    """Cap-state → tmp. Замок active-run выводится из этого же пути → тоже в tmp."""
    state_file = tmp_path / "budget_daily_cap_state.json"
    monkeypatch.setattr(budget_daily_cap, "_CAP_STATE_FILE", state_file)
    return state_file


# ===========================================================================
# 1B.2 — протокол reservation → provider → commit (unit, детерминированно)
# ===========================================================================

class TestReservationProtocol:
    def test_success_commit(self):
        """mutate → True (успех) → committed, raised_pct учтён, remaining уменьшился."""
        calls = []

        def mutate():
            calls.append(1)
            return True

        res = budget_daily_cap.apply_capped_raise("a1", 100.0, 110.0, 15.0, mutate, now=_now())
        assert res["status"] == "committed"
        assert calls == [1]
        # 10% зафиксировано → remaining = 5%
        remaining = budget_daily_cap.get_remaining_daily_pct("a1", 110.0, 15.0, now=_now())
        assert remaining == pytest.approx(5.0)

    def test_rejected_release(self):
        """mutate → False (явный отказ FB, бюджет не менялся) → reservation снята."""
        res = budget_daily_cap.apply_capped_raise("a1", 100.0, 110.0, 15.0, lambda: False, now=_now())
        assert res["status"] == "rejected"
        # Резервация освобождена → лимит снова полный
        remaining = budget_daily_cap.get_remaining_daily_pct("a1", 100.0, 15.0, now=_now())
        assert remaining == pytest.approx(15.0)

    def test_unknown_exception_keeps_pending_and_blocks_repeat(self):
        """mutate бросает исключение (timeout) → pending удержан, повтор заблокирован."""
        def boom():
            raise TimeoutError("FB не ответил")

        res = budget_daily_cap.apply_capped_raise("a1", 100.0, 110.0, 15.0, boom, now=_now())
        assert res["status"] == "pending"
        # pending=10% держит лимит: remaining = 5%
        remaining = budget_daily_cap.get_remaining_daily_pct("a1", 100.0, 15.0, now=_now())
        assert remaining == pytest.approx(5.0)

        # Повтор того же подъёма (10%) не влезает в остаток 5% → capped, FB не зовём
        calls = []
        res2 = budget_daily_cap.apply_capped_raise(
            "a1", 100.0, 110.0, 15.0, lambda: calls.append(1) or True, now=_now(),
        )
        assert res2["status"] == "capped"
        assert calls == []  # mutate НЕ вызывался

    def test_unknown_string_outcome_keeps_pending(self):
        """mutate → 'unknown' → pending удержан (без исключения)."""
        res = budget_daily_cap.apply_capped_raise("a1", 100.0, 110.0, 15.0, lambda: "unknown", now=_now())
        assert res["status"] == "pending"

    def test_reserve_over_cap_returns_none(self):
        """Резервация сверх остатка капа → None, FB не трогаем."""
        # Первая: 10% (влезает)
        op1 = budget_daily_cap.reserve_raise("a1", 100.0, 110.0, 15.0, now=_now())
        assert op1 is not None
        # Вторая: ещё 10% (10+10=20 > 15) → None
        op2 = budget_daily_cap.reserve_raise("a1", 100.0, 110.0, 15.0, now=_now())
        assert op2 is None

    def test_commit_then_reserve_respects_committed(self):
        """После commit 10% следующая резервация >5% отклоняется."""
        op1 = budget_daily_cap.reserve_raise("a1", 100.0, 110.0, 15.0, now=_now())
        budget_daily_cap.commit_reservation("a1", op1, now=_now())
        # осталось 5% → 10% не влезает
        op2 = budget_daily_cap.reserve_raise("a1", 100.0, 110.0, 15.0, now=_now())
        assert op2 is None
        # а 5% влезает
        op3 = budget_daily_cap.reserve_raise("a1", 100.0, 105.0, 15.0, now=_now())
        assert op3 is not None

    @pytest.mark.parametrize("bad_new", [float("inf"), float("nan"), -50.0, 0.0])
    def test_invalid_budget_no_reservation(self, bad_new):
        """Невалидный (не finite/неположительный) new_budget → None, без резервации."""
        op = budget_daily_cap.reserve_raise("a1", 100.0, bad_new, 15.0, now=_now())
        assert op is None

    def test_apply_capped_commit_fail_keeps_pending(self):
        """FB-успех, но локальный commit упал → pending остаётся (кап защищён)."""
        # reserve отработает, mutate=True, а commit_reservation бросит
        with patch.object(budget_daily_cap, "commit_reservation", side_effect=OSError("disk")):
            res = budget_daily_cap.apply_capped_raise("a1", 100.0, 110.0, 15.0, lambda: True, now=_now())
        assert res["status"] == "pending"
        # pending всё ещё держит лимит
        remaining = budget_daily_cap.get_remaining_daily_pct("a1", 100.0, 15.0, now=_now())
        assert remaining == pytest.approx(5.0)


# ===========================================================================
# 1B.2 — reconciliation (сверка зависших pending)
# ===========================================================================

class TestReconciliation:
    def test_reconcile_success_commits(self):
        """Фактический бюджет == new → FB применил → commit."""
        op = budget_daily_cap.reserve_raise("a1", 100.0, 110.0, 15.0, now=_now())
        assert op is not None
        res = budget_daily_cap.reconcile_pending("a1", 110.0, now=_now())
        assert res == {"committed": 1, "released": 0, "kept": 0}
        # committed → raised_pct=10, pending пуст
        remaining = budget_daily_cap.get_remaining_daily_pct("a1", 110.0, 15.0, now=_now())
        assert remaining == pytest.approx(5.0)
        assert budget_daily_cap.list_pending_adsets() == []

    def test_reconcile_unchanged_releases(self):
        """Фактический бюджет == old → FB не применял → release."""
        op = budget_daily_cap.reserve_raise("a1", 100.0, 110.0, 15.0, now=_now())
        assert op is not None
        res = budget_daily_cap.reconcile_pending("a1", 100.0, now=_now())
        assert res == {"committed": 0, "released": 1, "kept": 0}
        remaining = budget_daily_cap.get_remaining_daily_pct("a1", 100.0, 15.0, now=_now())
        assert remaining == pytest.approx(15.0)
        assert budget_daily_cap.list_pending_adsets() == []

    def test_reconcile_ambiguous_keeps(self):
        """Фактический бюджет иной (неоднозначно) → pending остаётся."""
        budget_daily_cap.reserve_raise("a1", 100.0, 110.0, 15.0, now=_now())
        res = budget_daily_cap.reconcile_pending("a1", 130.0, now=_now())
        assert res == {"committed": 0, "released": 0, "kept": 1}
        assert budget_daily_cap.list_pending_adsets() == ["a1"]

    def test_reconcile_none_actual_keeps(self):
        """Фактический бюджет недоступен (None) → pending остаётся (fail-closed)."""
        budget_daily_cap.reserve_raise("a1", 100.0, 110.0, 15.0, now=_now())
        res = budget_daily_cap.reconcile_pending("a1", None, now=_now())
        assert res["kept"] == 1
        assert budget_daily_cap.list_pending_adsets() == ["a1"]


# ===========================================================================
# 1B.2 — смена дня CityA и fail-closed по битому/несохраняемому state
# ===========================================================================

class TestDayRolloverAndFailClosed:
    def test_pending_counts_same_day(self):
        """В тот же день pending НЕ превращается в свободный лимит."""
        budget_daily_cap.reserve_raise("a1", 100.0, 114.0, 15.0, now=_now())  # 14%
        remaining = budget_daily_cap.get_remaining_daily_pct("a1", 100.0, 15.0, now=_now())
        assert remaining == pytest.approx(1.0)

    def test_new_day_resets_cap(self):
        """Смена календарного дня CityA → лимит на новый день полный (anchored к текущему)."""
        yesterday = _now("2026-07-01")
        today = _now("2026-07-02")
        op = budget_daily_cap.reserve_raise("a1", 100.0, 114.0, 15.0, now=yesterday)  # 14%
        budget_daily_cap.commit_reservation("a1", op, now=yesterday)
        # Вчера лимит почти исчерпан
        assert budget_daily_cap.get_remaining_daily_pct("a1", 114.0, 15.0, now=yesterday) == pytest.approx(1.0)
        # Новый день — свежий кап (rollover)
        remaining = budget_daily_cap.get_remaining_daily_pct("a1", 114.0, 15.0, now=today)
        assert remaining == pytest.approx(15.0)

    def test_corrupt_state_active_fail_closed(self, isolated_cap):
        """Битый cap-state в денежном пути (reserve) → None, mutate НЕ вызывается."""
        isolated_cap.parent.mkdir(parents=True, exist_ok=True)
        isolated_cap.write_text("{битый json", encoding="utf-8")

        calls = []
        res = budget_daily_cap.apply_capped_raise(
            "a1", 100.0, 110.0, 15.0, lambda: calls.append(1) or True, now=_now(),
        )
        assert res["status"] == "capped"
        assert calls == []  # 0 FB-мутаций

    def test_save_error_fail_closed(self):
        """Ошибка сохранения cap-state → reserve None, mutate НЕ вызывается."""
        calls = []
        with patch.object(budget_daily_cap, "_save_cap_state", side_effect=OSError("no space")):
            res = budget_daily_cap.apply_capped_raise(
                "a1", 100.0, 110.0, 15.0, lambda: calls.append(1) or True, now=_now(),
            )
        assert res["status"] == "capped"
        assert calls == []


# ===========================================================================
# 1B.2 — два потока резервируют один adset (in-process) → сумма ≤ cap
# ===========================================================================

class TestTwoThreadsReservation:
    def test_two_threads_one_adset_sum_within_cap(self):
        """Два потока одновременно резервируют по 10% одного адсета (cap 15%) —
        ровно один получает op_id, суммарно pending+committed ≤ cap."""
        barrier = threading.Barrier(2)
        results: dict[str, str | None] = {}

        def worker(name):
            barrier.wait(timeout=5)
            results[name] = budget_daily_cap.reserve_raise("a1", 100.0, 110.0, 15.0, now=_now())

        t1 = threading.Thread(target=worker, args=("t1",))
        t2 = threading.Thread(target=worker, args=("t2",))
        t1.start()
        t2.start()
        t1.join(5)
        t2.join(5)

        got = [v for v in results.values() if v is not None]
        assert len(got) == 1  # ровно одна резервация прошла

        state = budget_daily_cap._load_cap_state()
        entry = state["adsets"]["a1"]
        total = float(entry.get("raised_pct", 0.0)) + budget_daily_cap._pending_pct(entry)
        assert total <= 15.0 + 1e-6


# ===========================================================================
# 1B.1 — один активный active-run (замок в run_budget_scaling)
# ===========================================================================

class TestActiveRunLock:
    def test_two_threads_exactly_one_active_run(self, monkeypatch):
        """Два потока зовут run_budget_scaling('active') — ровно один входит в
        _run_scaling_inner, второй получает skipped_reason='active_run_in_progress'."""
        import services.budget_scaler as scaler

        entered: list[int] = []
        hold = threading.Event()

        def fake_inner(mode, max_scales):
            entered.append(1)
            hold.wait(timeout=3)  # держим замок, пока второй поток пробует
            return {
                "ran": True, "skipped_reason": None, "mode": mode,
                "winners": [], "recommendations": [], "scaled": [], "errors": [],
            }

        monkeypatch.setattr(scaler, "_run_scaling_inner", fake_inner)

        results: dict[str, dict] = {}

        def worker(name):
            results[name] = scaler.run_budget_scaling(mode="active")

        t1 = threading.Thread(target=worker, args=("a",))
        t1.start()
        # ждём, пока t1 зайдёт внутрь и будет держать замок
        for _ in range(300):
            if entered:
                break
            time.sleep(0.01)
        assert entered, "первый поток не успел войти в inner"

        # второй вызов, пока первый держит замок → skip
        results["b"] = scaler.run_budget_scaling(mode="active")
        hold.set()
        t1.join(5)

        skipped = [r.get("skipped_reason") for r in results.values()]
        assert skipped.count("active_run_in_progress") == 1
        assert entered.count(1) == 1  # inner отработал ровно один раз

    def test_stale_timedout_worker_blocks_second_tick(self, monkeypatch):
        """Старый worker (переполз по future.result-таймауту) ещё держит замок →
        следующий tick НЕ вызывает set_adset_budget."""
        import services.budget_scaler as scaler

        stale = scaler._try_acquire_active_run_lock()
        assert stale is not None
        try:
            with patch("services.budget_scaler.set_adset_budget") as mock_set:
                result = scaler.run_budget_scaling(mode="active")
            assert result["ran"] is False
            assert result["skipped_reason"] == "active_run_in_progress"
            mock_set.assert_not_called()
        finally:
            stale.release()

        # после освобождения — замок снова берётся
        again = scaler._try_acquire_active_run_lock()
        assert again is not None
        again.release()

    def test_dry_run_does_not_take_active_lock(self, monkeypatch):
        """dry_run не конкурирует за mutation-lock: даже когда замок занят, dry_run идёт."""
        import services.budget_scaler as scaler

        entered = []

        def fake_inner(mode, max_scales):
            entered.append(mode)
            return {
                "ran": True, "skipped_reason": None, "mode": mode,
                "winners": [], "recommendations": [], "scaled": [], "errors": [],
            }

        monkeypatch.setattr(scaler, "_run_scaling_inner", fake_inner)

        held = scaler._try_acquire_active_run_lock()
        assert held is not None
        try:
            result = scaler.run_budget_scaling(mode="dry_run")
        finally:
            held.release()

        assert result["ran"] is True
        assert entered == ["dry_run"]  # dry_run прошёл, несмотря на занятый замок

    def test_lock_acquire_error_fail_closed(self, monkeypatch):
        """Ошибка захвата замка (os.open бросил) → None (fail-closed), tlock освобождён."""
        import services.budget_scaler as scaler

        def boom(*a, **k):
            raise OSError("cannot open lock")

        monkeypatch.setattr(scaler.os, "open", boom)
        lock = scaler._try_acquire_active_run_lock()
        assert lock is None
        # threading-lock не залип — можно взять снова
        assert scaler._ACTIVE_RUN_TLOCK.acquire(blocking=False) is True
        scaler._ACTIVE_RUN_TLOCK.release()

    def test_run_active_skips_when_lock_unavailable(self, monkeypatch):
        """run_budget_scaling('active') при недоступном замке → skip, 0 FB."""
        import services.budget_scaler as scaler

        monkeypatch.setattr(scaler, "_try_acquire_active_run_lock", lambda: None)
        with patch("services.budget_scaler.set_adset_budget") as mock_set:
            result = scaler.run_budget_scaling(mode="active")
        assert result["skipped_reason"] == "active_run_in_progress"
        mock_set.assert_not_called()


# ===========================================================================
# 1B.1 / 1B.2 — межпроцессные проверки (spawn + flock)
# ===========================================================================

def _mp_flock_holder(lock_path, acquired_evt, release_evt, result_q):
    import fcntl as _f
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            _f.flock(fd, _f.LOCK_EX | _f.LOCK_NB)
        except OSError:
            result_q.put(("holder", False))
            return
        result_q.put(("holder", True))
        acquired_evt.set()
        release_evt.wait(timeout=10)
        _f.flock(fd, _f.LOCK_UN)
    finally:
        os.close(fd)


def _mp_flock_trier(lock_path, acquired_evt, result_q):
    import fcntl as _f
    acquired_evt.wait(timeout=10)  # пробуем ТОЛЬКО когда holder уже держит
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            _f.flock(fd, _f.LOCK_EX | _f.LOCK_NB)
            result_q.put(("trier", True))
            _f.flock(fd, _f.LOCK_UN)
        except OSError:
            result_q.put(("trier", False))
    finally:
        os.close(fd)


def _mp_reserve_worker(cap_state_path, ready_evt, go_evt, result_q):
    import services.budget_daily_cap as cap
    cap._CAP_STATE_FILE = Path(cap_state_path)
    ready_evt.set()
    go_evt.wait(timeout=10)
    op = cap.reserve_raise("adsetX", 100.0, 110.0, 15.0)  # 10% каждый
    result_q.put(op is not None)


@_requires_fcntl
class TestTwoProcesses:
    def test_two_processes_one_holds_active_lock(self):
        """Два процесса на одном .lock-файле active-run → держит ровно один (flock)."""
        import services.budget_scaler as scaler

        lock_path = str(scaler._active_run_lock_path())
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)

        mp = multiprocessing.get_context("spawn")
        acquired = mp.Event()
        release = mp.Event()
        q = mp.Queue()

        holder = mp.Process(target=_mp_flock_holder, args=(lock_path, acquired, release, q))
        trier = mp.Process(target=_mp_flock_trier, args=(lock_path, acquired, q))
        holder.start()
        trier.start()
        try:
            results = dict(q.get(timeout=20) for _ in range(2))
        finally:
            release.set()
            holder.join(10)
            trier.join(10)

        assert results.get("holder") is True
        assert results.get("trier") is False  # второй процесс не смог взять замок

    def test_two_processes_reserve_same_adset_within_cap(self, tmp_path):
        """Два процесса резервируют один adset (по 10%, cap 15%) → ровно один
        успех, на диске pending+committed ≤ cap."""
        cap_path = str(tmp_path / "budget_daily_cap_state.json")

        mp = multiprocessing.get_context("spawn")
        q = mp.Queue()
        r1 = mp.Event()
        r2 = mp.Event()
        go = mp.Event()

        p1 = mp.Process(target=_mp_reserve_worker, args=(cap_path, r1, go, q))
        p2 = mp.Process(target=_mp_reserve_worker, args=(cap_path, r2, go, q))
        p1.start()
        p2.start()
        try:
            assert r1.wait(timeout=15)
            assert r2.wait(timeout=15)
            go.set()  # оба стартуют резервацию почти одновременно
            res = [q.get(timeout=15), q.get(timeout=15)]
        finally:
            p1.join(10)
            p2.join(10)

        assert sum(1 for x in res if x) == 1  # ровно одна резервация прошла

        # Проверяем инвариант на диске
        budget_daily_cap._CAP_STATE_FILE = Path(cap_path)
        state = budget_daily_cap._load_cap_state()
        entry = state["adsets"]["adsetX"]
        total = float(entry.get("raised_pct", 0.0)) + budget_daily_cap._pending_pct(entry)
        assert total <= 15.0 + 1e-6
