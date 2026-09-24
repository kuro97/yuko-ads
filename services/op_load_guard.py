"""Страж загрузки отдела продаж: «не обработано» ≠ «плохие заявки».

Зачем. Правило B раннего стопа (services/early_kill.py) паузит объявление, у
которого зрелые заявки (старше 72 ч) есть, а квалов нет. Если отдел продаж
завален и до заявок не дотягивается, квалов нет по всему кабинету — и правило
режет живую рекламу. Принцип: если заявку не обработали, а деньги на неё уже
потрачены — объявление держать дольше.

Что меряем. Раз в час берём заявки, созданные 72–96 часов назад (ровно те,
что уже «созрели» для правила B), и считаем долю тронутых менеджером: статус
ушёл дальше «НОВАЯ ЗАЯВКА» / «ОТВЕТСТВЕННЫЙ НАЗНАЧЕН» (автораздача статус
двигает сама, поэтому он не считается касанием). Замер пишется в
data/op_load_guard.json, базовая линия — медиана замеров за 14 дней.

Вердикт «ОП не успевает»: замеров ≥ MIN_SAMPLES, в окне ≥ MIN_LEADS заявок и
доля тронутых ниже BASELINE_RATIO × базовой линии. Без базовой линии страж
не блокирует (нечего сравнивать), но замер копит. Любая ошибка AMO → «неизвестно»,
и правило B ждёт: недоказанное не режется.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = _ROOT / "data" / "op_load_guard.json"
_TZ_LOCAL = timezone(timedelta(hours=5))

WINDOW_FROM_HOURS = 96
WINDOW_TO_HOURS = 72
SAMPLE_TTL_MINUTES = 50          # чаще раза в час AMO не спрашиваем
BASELINE_DAYS = 14
MIN_SAMPLES = 24                 # сутки замеров прежде чем страж вправе блокировать
MIN_LEADS = 20
BASELINE_RATIO = 0.5
_PAGE_LIMIT = 250
_MAX_PAGES = 10

# Статусы, в которых заявка ещё никем не тронута: «НОВАЯ ЗАЯВКА» и
# «ОТВЕТСТВЕННЫЙ НАЗНАЧЕН» (автораздача). Переопределяются переменной окружения.
_DEFAULT_UNTOUCHED = "32364429,44520175"


def untouched_status_ids() -> frozenset[int]:
    raw = os.getenv("AMO_UNTOUCHED_STATUS_IDS", _DEFAULT_UNTOUCHED)
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class OpLoadVerdict:
    ok: bool                 # True = правило B может паузить
    reason: str              # ok | overloaded | no_baseline | few_leads | unknown
    share: float | None      # доля тронутых в окне
    baseline: float | None   # медиана за 14 дней
    leads: int               # заявок в окне
    sampled_at: str | None


def _fetch_window_share(now: datetime) -> tuple[float | None, int]:
    """Доля тронутых заявок среди созданных 72–96 ч назад. (None, 0) при ошибке."""
    from integrations.amo import _amo_get, _is_service_lead
    import config

    since = int((now - timedelta(hours=WINDOW_FROM_HOURS)).timestamp())
    until = int((now - timedelta(hours=WINDOW_TO_HOURS)).timestamp())
    untouched = untouched_status_ids()
    total = touched = 0
    page = 1
    while page <= _MAX_PAGES:
        params: dict = {
            "filter[created_at][from]": since,
            "filter[created_at][to]": until,
            "with": "tags",
            "limit": _PAGE_LIMIT,
            "page": page,
        }
        pipeline = getattr(config, "AMO_PIPELINE_ID", None)
        if pipeline:
            params["filter[pipeline_id]"] = pipeline
        payload = _amo_get("leads", params)
        rows = ((payload or {}).get("_embedded") or {}).get("leads") or []
        for lead in rows:
            normalized = {
                "custom_fields": lead.get("custom_fields_values") or [],
                "tags": ((lead.get("_embedded") or {}).get("tags")) or [],
            }
            if _is_service_lead(normalized):
                continue
            total += 1
            try:
                status = int(lead.get("status_id") or 0)
            except (TypeError, ValueError):
                status = 0
            if status not in untouched:
                touched += 1
        if len(rows) < _PAGE_LIMIT:
            break
        page += 1
    if total == 0:
        return None, 0
    return touched / total, total


def _load_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("op_load_guard: state не сохранён — %s", exc)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def check_op_load(*, now: datetime | None = None, state_path: Path | None = None) -> OpLoadVerdict:
    """Главная точка: свежий замер (не чаще раза в час) + вердикт. Никогда не бросает."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    path = state_path or STATE_FILE
    state = _load_state(path)
    samples: list[dict] = [s for s in state.get("samples", []) if isinstance(s, dict)]
    cutoff = moment - timedelta(days=BASELINE_DAYS)
    samples = [s for s in samples if (_parse(s.get("at")) or cutoff) >= cutoff]

    latest = samples[-1] if samples else None
    latest_at = _parse(latest.get("at")) if latest else None
    if latest_at is None or (moment - latest_at) > timedelta(minutes=SAMPLE_TTL_MINUTES):
        try:
            share, leads = _fetch_window_share(moment)
        except Exception as exc:  # noqa: BLE001 — AMO недоступен → неизвестно
            logger.warning("op_load_guard: AMO недоступен — %s", str(exc)[:100])
            return OpLoadVerdict(False, "unknown", None, None, 0, latest.get("at") if latest else None)
        latest = {"at": moment.isoformat(), "share": share, "leads": leads}
        samples.append(latest)
        _save_state(path, {"samples": samples[-24 * BASELINE_DAYS:]})

    share = latest.get("share")
    leads = int(latest.get("leads") or 0)
    history = [
        float(s["share"]) for s in samples[:-1]
        if s.get("share") is not None and int(s.get("leads") or 0) >= MIN_LEADS
    ]
    baseline = statistics.median(history) if len(history) >= MIN_SAMPLES else None
    if share is None or leads < MIN_LEADS:
        return OpLoadVerdict(True, "few_leads", share, baseline, leads, latest["at"])
    if baseline is None:
        return OpLoadVerdict(True, "no_baseline", float(share), None, leads, latest["at"])
    if float(share) < BASELINE_RATIO * baseline:
        return OpLoadVerdict(False, "overloaded", float(share), baseline, leads, latest["at"])
    return OpLoadVerdict(True, "ok", float(share), baseline, leads, latest["at"])
