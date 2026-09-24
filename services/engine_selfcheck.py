"""Самопроверка прогнозного движка CDP для Budget Scaler (Этап 4, «CDP аккуратно»).

Зачем: CDP-прогноз (forecast_eom) считает доход по квал-лидам и может завышать.
WAPE-предохранитель месячный и ловит свежую смену методики с лагом. Здесь —
независимая недельная сверка: «что прогноз обещал ~неделю назад к сегодняшнему
дню (с поправкой на сезонную кривую) vs фактическая выручка сегодня». Сильный
недобор факта → недоверие движку (откат на сезонную кривую, как при WAPE) +
человеческое сомнение владельцу.

Ежедневный снапшот (record_snapshot) копит (forecast_eom, fact_mtd) из
budget-context. Первый осмысленный вердикт — через ~6-9 дней накопления.

State-файл: data/engine_selfcheck_state.json
Формат: {"snapshots": [{"date": "YYYY-MM-DD", "month": "YYYY-MM",
                         "forecast_eom": float, "fact_mtd": float}, ...]}

Wave 3C (fail-closed): самопроверка — ПРЕДОХРАНИТЕЛЬ active-подъёма бюджета.
CDP-ошибки НЕ роняют прогон (функции не бросают наружу — возвращают статус),
но и НЕ трактуются как «всё ок»: любой сбой (недоступный/неполный/битый ответ,
ошибка сохранения, битый state, нехватка истории, ошибка сверки/сезонной кривой)
→ самопроверка НЕ пропускает active-подъём (gate_ok=False). Снапшот сохраняется
как валидный ТОЛЬКО из полного ответа с finite/неотрицательными полями за текущий
месяц/день. Ошибка сохранения возвращается вызывающему ЯВНО (status="save_error"),
а не скрывается за логом. Оркестратор для гейта — run_selfcheck().
"""

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services import cdp_client, pacing_curve
from services.cdp_client import CdpError
from services.formatting import fmt_money

logger = logging.getLogger(__name__)

_TZ_LOCAL = timezone(timedelta(hours=5))

_SELFCHECK_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "engine_selfcheck_state.json"

# Ретеншн снапшотов (дни)
_SNAPSHOT_RETENTION_DAYS = 60

# Окно поиска «прошлого» снапшота для сверки (дни назад, включительно)
_LOOKBACK_MIN_DAYS = 6
_LOOKBACK_MAX_DAYS = 9
_LOOKBACK_TARGET_DAYS = 7  # предпочтительная давность (≈«неделю назад»)


def _now(now: datetime | None = None) -> datetime:
    """Нормализует момент к aware-datetime TZ CityA."""
    moment = now if now is not None else datetime.now(_TZ_LOCAL)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=_TZ_LOCAL)
    return moment.astimezone(_TZ_LOCAL)


def _read_state_raw() -> tuple[dict, bool]:
    """Читает state. Возвращает (state, corrupt).

    corrupt=True ТОЛЬКО если файл существует, но не читается / не JSON / неверной
    формы (snapshots не список). Отсутствие файла — НЕ corrupt (чистый прогрев).
    Wave 3C: битый state нельзя молча трактовать как чистый новый — вызывающий
    обязан отличить эти случаи (fail-closed до восстановления).
    """
    default: dict = {"snapshots": []}
    if not _SELFCHECK_STATE_FILE.exists():
        return default, False
    try:
        data = json.loads(_SELFCHECK_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("engine_selfcheck: битый state (не прочитать) — %s", exc)
        return default, True
    if not isinstance(data, dict) or not isinstance(data.get("snapshots"), list):
        logger.warning("engine_selfcheck: битый state (неверная форма)")
        return default, True
    return {"snapshots": data["snapshots"]}, False


def _load_state() -> dict:
    """Читает state (толерантно). При ошибке/отсутствии/битом → {'snapshots': []}."""
    state, _ = _read_state_raw()
    return state


def _save_state(state: dict) -> None:
    """Атомарно (tmp+rename) сохраняет state.

    Wave 3C: ошибку НЕ глотаем — пробрасываем наверх, чтобы record_snapshot вернул
    вызывающему явный failure (иначе неудачное сохранение выглядело бы как успех
    → fail-open разрешение подъёма).
    """
    _SELFCHECK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SELFCHECK_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_SELFCHECK_STATE_FILE)


def _fnum(x) -> float | None:
    """float(x) или None."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _finite_nonneg(x) -> float | None:
    """float(x), если это finite и неотрицательное число, иначе None.

    Отсекает None, NaN, ±Infinity и отрицательные значения — обязательные признаки
    валидного поля снапшота (Wave 3C).
    """
    v = _fnum(x)
    if v is None or not math.isfinite(v) or v < 0:
        return None
    return v


def _is_valid_snapshot(
    snap, expected_month: str | None = None, expected_date: str | None = None
) -> bool:
    """Строгая валидация снапшота (Wave 3C).

    Валиден только если: это dict; date парсится как ISO-дата (и == expected_date,
    если задан); forecast_eom и fact_mtd присутствуют, finite и неотрицательны;
    month снапшота == expected_month (если задан).
    """
    if not isinstance(snap, dict):
        return False
    d = str(snap.get("date") or "")
    try:
        datetime.fromisoformat(d)
    except (ValueError, TypeError):
        return False
    if expected_date is not None and d != expected_date:
        return False
    if _finite_nonneg(snap.get("forecast_eom")) is None:
        return False
    if _finite_nonneg(snap.get("fact_mtd")) is None:
        return False
    if expected_month is not None and _snap_month(snap) != expected_month:
        return False
    return True


def _extract_valid_snapshot(ctx, now_dt: datetime) -> dict | None:
    """Достаёт ВАЛИДНЫЙ снапшот из полного ответа budget-context или None.

    Wave 3C: неполный/битый ответ (не dict, нет revenue_new._total, поля не
    finite/отрицательны, месяц не текущий) → None (не сохраняем как валидный).
    """
    if not isinstance(ctx, dict):
        return None
    rev = _total_revenue_new(ctx)
    if rev is None:
        return None
    forecast = _finite_nonneg(rev.get("forecast_eom"))
    fact = _finite_nonneg(rev.get("fact_mtd"))
    if forecast is None or fact is None:
        return None
    month_now = now_dt.strftime("%Y-%m")
    month = str(ctx.get("month") or month_now)
    # Ответ должен относиться к ТЕКУЩЕМУ месяцу (рассинхрон = неполный/несогласованный)
    if month != month_now:
        return None
    return {
        "date": now_dt.date().isoformat(),
        "month": month,
        "forecast_eom": forecast,
        "fact_mtd": fact,
    }


def _total_revenue_new(ctx: dict) -> dict | None:
    """metrics.revenue_new агрегата city=='_total'. None если нет."""
    for c in (ctx.get("cities") or []):
        if not isinstance(c, dict):
            continue
        if c.get("city") == "_total":
            m = (c.get("metrics") or {}).get("revenue_new")
            return m if isinstance(m, dict) else None
    return None


def record_snapshot(now: datetime | None = None) -> dict:
    """Раз в день кладёт ВАЛИДНЫЙ снапшот (date, month, forecast_eom, fact_mtd) из
    budget-context.

    Wave 3C (fail-closed):
    - Уже есть ВАЛИДНЫЙ снапшот сегодня → идемпотентно (без дубля и без CDP-запроса).
    - НЕвалидный снапшот сегодня можно заменить валидным ответом в тот же день.
    - Неполный/битый ответ CDP → НЕ сохраняем (status="invalid").
    - CDP недоступен → status="unavailable".
    - Ошибка сохранения → status="save_error" (ЯВНЫЙ failure, не скрыт за логом).
    - Ретеншн 60 дней.

    Returns:
        {"status": "recorded"|"idempotent"|"invalid"|"unavailable"|"save_error",
         "saved": bool}
    """
    now_dt = _now(now)
    today = now_dt.date().isoformat()
    month_now = now_dt.strftime("%Y-%m")

    state = _load_state()
    snapshots = state["snapshots"]

    # Уже есть ВАЛИДНЫЙ снапшот сегодня → идемпотентно (не трогаем CDP, без дубля)
    if any(_is_valid_snapshot(s, month_now, today) for s in snapshots):
        return {"status": "idempotent", "saved": False}

    try:
        ctx = cdp_client.get_budget_context()
    except CdpError as exc:
        logger.info("record_snapshot: budget-context недоступен — %s", exc)
        return {"status": "unavailable", "saved": False}
    except Exception as exc:  # двойная страховка — снапшот не роняет прогон
        logger.warning("record_snapshot: неожиданная ошибка budget-context — %s", exc)
        return {"status": "unavailable", "saved": False}

    snap = _extract_valid_snapshot(ctx, now_dt)
    if snap is None:
        logger.info("record_snapshot: неполный/битый ответ CDP — снапшот невалиден, не сохраняем")
        return {"status": "invalid", "saved": False}

    # Заменяем ЛЮБОЙ сегодняшний снапшот (в т.ч. невалидный) на новый валидный
    kept = [s for s in snapshots if not (isinstance(s, dict) and s.get("date") == today)]
    kept.append(snap)

    # Ретеншн: выкидываем снапшоты старше 60 дней
    cutoff = (now_dt - timedelta(days=_SNAPSHOT_RETENTION_DAYS)).date().isoformat()
    state["snapshots"] = [
        s for s in kept
        if isinstance(s, dict) and str(s.get("date") or "") >= cutoff
    ]

    try:
        _save_state(state)
    except Exception as exc:
        logger.error("record_snapshot: не удалось сохранить snapshot — %s", exc)
        return {"status": "save_error", "saved": False}
    return {"status": "recorded", "saved": True}


def _snap_month(snap: dict) -> str:
    """Месяц снапшота ('YYYY-MM'): из поля month, иначе из date."""
    m = snap.get("month")
    if m:
        return str(m)
    d = str(snap.get("date") or "")
    return d[:7]


def _pick_then_snapshot(snapshots: list, now_dt: datetime, month_now: str) -> tuple[dict, int] | None:
    """Ищет снапшот 6-9 дней назад ТОГО ЖЕ месяца, ближайший к 7 дням.

    Returns:
        (снапшот, age_days) или None.
    """
    today = now_dt.date()
    best: tuple[dict, int] | None = None
    best_key: tuple[int, int] | None = None
    for s in snapshots:
        # Wave 3C: рассматриваем только СТРОГО валидные снапшоты текущего месяца
        if not _is_valid_snapshot(s, month_now):
            continue
        d_raw = s.get("date")
        try:
            d = datetime.fromisoformat(str(d_raw)).date() if d_raw else None
        except ValueError:
            d = None
        if d is None:
            continue
        age = (today - d).days
        if age < _LOOKBACK_MIN_DAYS or age > _LOOKBACK_MAX_DAYS:
            continue
        # Ближе к 7 дням лучше; при равенстве — более старый (больший age)
        key = (abs(age - _LOOKBACK_TARGET_DAYS), -age)
        if best_key is None or key < best_key:
            best = (s, age)
            best_key = key
    return best


def compute_forecast_selfcheck(now: datetime | None = None, max_dev: float = 0.25) -> dict | None:
    """Сверяет обещание прогноза ~недельной давности с фактом сегодня, с поправкой
    на сезонную кривую.

    Ожидаемый факт на сегодня из «тогдашнего» снапшота:
        expected_gain = (forecast_then − fact_then) × (share_now − share_then) / (1 − share_then)
        expected_now  = fact_then + expected_gain
    где share_* — pacing_curve.expected_cumulative_share на соответствующий день.

    Недоверие (reason задан), если факт сегодня ниже ожидания больше, чем на max_dev.

    Returns:
        {"reason": str|None, "dev": float, "expected_now": float, "fact_now": float,
         "days_ago": int} — или None, если сверку провести нельзя (нет снапшотов,
        деление на 0, дни 1-4 месяца и т.п.).
    """
    now_dt = _now(now)
    today = now_dt.date().isoformat()
    month_now = now_dt.strftime("%Y-%m")

    state = _load_state()
    snapshots = state.get("snapshots", [])

    # Факт сегодня — из ВАЛИДНОГО сегодняшнего снапшота (record_snapshot зовётся до сверки)
    today_snap = next(
        (s for s in snapshots if _is_valid_snapshot(s, month_now, today)), None
    )
    if today_snap is None:
        return None
    fact_now = _fnum(today_snap.get("fact_mtd"))
    if fact_now is None:
        return None

    picked = _pick_then_snapshot(snapshots, now_dt, month_now)
    if picked is None:
        return None
    then_snap, age_days = picked

    forecast_then = _fnum(then_snap.get("forecast_eom"))
    fact_then = _fnum(then_snap.get("fact_mtd"))
    if forecast_then is None or fact_then is None:
        return None

    # Сезонные доли: сегодня и «тогда»
    try:
        then_date = datetime.fromisoformat(str(then_snap.get("date")))
    except (ValueError, TypeError):
        return None
    if then_date.tzinfo is None:
        then_date = then_date.replace(tzinfo=_TZ_LOCAL)

    share_now = pacing_curve.expected_cumulative_share(now_dt)
    share_then = pacing_curve.expected_cumulative_share(then_date)

    # Дни 1-4 (share=0) / деление на ноль / отсутствие прироста → сверку не делаем
    if share_now <= 0.0:
        return None
    if (1.0 - share_then) <= 0.0:
        return None
    if (share_now - share_then) <= 0.0:
        return None

    expected_gain = (forecast_then - fact_then) * (share_now - share_then) / (1.0 - share_then)
    expected_now = fact_then + expected_gain
    if expected_now <= 0:
        return None

    dev = (fact_now - expected_now) / expected_now  # < 0 когда факт отстаёт

    reason = None
    if fact_now < expected_now * (1.0 - max_dev):
        z_pct = (expected_now - fact_now) / expected_now * 100.0
        reason = (
            f"прогноз CDP завышает: неделю назад обещал {fmt_money(expected_now, '¤')} "
            f"к сегодня, факт {fmt_money(fact_now, '¤')} (−{z_pct:.0f}%)"
        )

    return {
        "reason": reason,
        "dev": dev,
        "expected_now": expected_now,
        "fact_now": fact_now,
        "days_ago": age_days,
    }


def evaluate_forecast_selfcheck(now: datetime | None = None, max_dev: float = 0.25) -> str | None:
    """Тонкая обёртка над compute_forecast_selfcheck: только причина недоверия
    (str) или None. Сигнатура для план-гейта Этапа 4."""
    result = compute_forecast_selfcheck(now, max_dev)
    return result["reason"] if result else None


def _gate_result(
    status: str,
    gate_ok: bool,
    reason: str | None = None,
    dev: float | None = None,
    skipped_reason: str | None = None,
) -> dict:
    """Унифицированный результат гейта самопроверки (Wave 3C)."""
    return {
        "status": status,
        "gate_ok": gate_ok,
        "reason": reason,
        "dev": dev,
        "skipped_reason": skipped_reason,
    }


def run_selfcheck(now: datetime | None = None, max_dev: float = 0.25) -> dict:
    """Fail-closed оркестратор самопроверки для active-гейта Бюджет-пилота (Wave 3C).

    Порядок: снапшот дня → детект битого state → недельная сверка. active-подъём
    разрешён самопроверкой (gate_ok=True) ТОЛЬКО при status="ok" (валидная история
    6-9 дней + отклонение факта в норме). ЛЮБОЙ иной исход блокирует подъём:
    - "unavailable" — CDP недоступен / ошибка сохранения / ошибка сверки / сезонной кривой;
    - "invalid"     — неполный ответ CDP / битый state (не «чистый новый»!);
    - "warming"     — недостаточно валидной истории для вердикта (прогрев);
    - "distrust"    — сильное отставание факта: недоверие движку (гасит engine-путь
                      через reason) + блок подъёма.

    Функция НИКОГДА не бросает наружу (внутренние сбои → status="unavailable"),
    но и не превращает ни один сбой в разрешение подъёма.

    Returns:
        {"status": str, "gate_ok": bool, "reason": str|None, "dev": float|None,
         "skipped_reason": str|None}
    """
    # 0. Детект битого state ДО записи (record_snapshot ниже толерантно перезапишет
    # today-запись — восстановление, — но битой истории в ЭТОМ прогоне не доверяем).
    _, corrupt = _read_state_raw()

    # 1. Снапшот дня. Сбой фетча/сохранения = блок (fail-closed), не «ок».
    try:
        rec = record_snapshot(now)
    except Exception as exc:  # запись снапшота не роняет прогон, но = блок
        logger.warning("run_selfcheck: record_snapshot упал неожиданно — %s", exc)
        return _gate_result("unavailable", False, skipped_reason="самопроверка: ошибка снапшота")

    rec_status = rec.get("status")
    if rec_status == "save_error":
        return _gate_result("unavailable", False, skipped_reason="самопроверка: ошибка сохранения снапшота")
    if rec_status == "unavailable":
        return _gate_result("unavailable", False, skipped_reason="самопроверка: CDP недоступен (нет свежего снапшота)")
    if rec_status == "invalid":
        return _gate_result("invalid", False, skipped_reason="самопроверка: неполный ответ CDP (снапшот невалиден)")

    # 2. Битый state НЕ трактуем как чистый новый: запись today выше начала
    # восстановление, но исторической сверке в этом прогоне не доверяем — блок.
    if corrupt:
        return _gate_result("invalid", False, skipped_reason="самопроверка: битый state (восстановление/прогрев)")

    # 3. Недельная сверка (evaluation + сезонная кривая). Ошибка → unavailable.
    try:
        sc = compute_forecast_selfcheck(now, max_dev)
    except Exception as exc:  # ошибка сверки/pacing_curve = блок
        logger.warning("run_selfcheck: сверка самопроверки упала — %s", exc)
        return _gate_result("unavailable", False, skipped_reason="самопроверка: ошибка сверки/сезонной кривой")

    if sc is None:
        # Недостаточно валидной истории 6-9 дней / сверку провести нельзя → прогрев.
        return _gate_result("warming", False, skipped_reason="самопроверка: недостаточно истории (прогрев)")

    if sc.get("reason"):
        # Сильное отставание: недоверие движку (reason гасит engine-путь) + блок подъёма.
        return _gate_result(
            "distrust", False, reason=sc["reason"], dev=sc.get("dev"),
            skipped_reason="самопроверка: прогноз завышает — недоверие движку",
        )

    # Валидная история + отклонение в норме → самопроверка ПРОПУСКАЕТ.
    # Прочие plan/unit/money-гейты продолжают действовать независимо.
    return _gate_result("ok", True, reason=None, dev=sc.get("dev"), skipped_reason=None)
