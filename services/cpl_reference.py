"""Скользящая цена лида кабинета — эталон для порогов раннего стопа.

Зачем. Цена лида меняется от месяца к месяцу (несезон против сезона),
поэтому порог «потратил N цен лида без заявки» не может быть
константой. Эталон считается по кабинету за скользящее окно (14 дней по
умолчанию) из FB insights уровня account: один дешёвый запрос на кабинет.

Почему кабинет, а не адсет и не заявки AMO (бэктест на исторической когорте):
  * адсетный эталон шумный — у многих объявлений в окне нет 10 лидов;
  * эталон по заявкам AMO работающих объявлений завышен и ловит мало;
  * FB-CPL кабинета ниже цены заявки AMO, и разрыв в каждом кабинете свой,
    поэтому множитель калибруется на кабинет и живёт в
    services/early_kill.py, а не здесь.

Модуль не принимает решений и не мутирует рекламу. Единственный побочный
эффект — кэш data/cpl_reference.json (пересчёт не чаще раза в max_age_hours).
Никогда не бросает наружу: отказ FB даёт эталон None с source="error", и
правило на нём fail-closed молчит.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = _ROOT / "data" / "cpl_reference.json"
_TZ_LOCAL = timezone(timedelta(hours=5))

DEFAULT_WINDOW_DAYS = 14
DEFAULT_MIN_LEADS = 10
DEFAULT_MAX_AGE_HOURS = 26.0  # пересчёт раз в сутки с запасом на сдвиг слота

_CACHEABLE_SOURCES = frozenset({"fb_account_window", "insufficient_leads"})


@dataclass(frozen=True)
class CplReference:
    """Эталон цены лида одного кабинета за окно."""

    account_id: str
    cpl_usd: str | None          # Decimal как строка (JSON-совместимо), None = эталона нет
    spend_usd: float
    leads: int
    window_days: int
    since: str                   # первая дата окна (CityA)
    until: str                   # последняя дата окна (CityA)
    computed_at: str             # ISO UTC
    source: str                  # fb_account_window | insufficient_leads | error

    @property
    def cpl(self) -> Decimal | None:
        return Decimal(self.cpl_usd) if self.cpl_usd is not None else None


def _window(now: datetime, window_days: int) -> tuple[str, str]:
    """Окно [сегодня − window_days, вчера] по датам CityA (только полные дни)."""
    today = now.astimezone(_TZ_LOCAL).date()
    until = today - timedelta(days=1)
    since = today - timedelta(days=window_days)
    return since.isoformat(), until.isoformat()


def fetch_account_cpl_reference(
    account_id: str,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_leads: int = DEFAULT_MIN_LEADS,
    now: datetime | None = None,
) -> CplReference:
    """Один запрос FB insights уровня account за окно. Никогда не бросает."""
    from agent.fb_common import API, _throttled_get
    from services.fb_token_provider import (
        fb_account,
        get_fb_account_id,
        get_fb_token,
        offline_account_context,
    )
    from services.meta_lead_actions import parse_meta_lead_actions

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    since, until = _window(moment, window_days)
    computed_at = moment.astimezone(timezone.utc).isoformat()
    account = str(account_id).replace("act_", "")

    def _result(source: str, *, spend: float = 0.0, leads: int = 0, cpl: Decimal | None = None) -> CplReference:
        return CplReference(
            account_id=account,
            cpl_usd=str(cpl) if cpl is not None else None,
            spend_usd=spend,
            leads=leads,
            window_days=window_days,
            since=since,
            until=until,
            computed_at=computed_at,
            source=source,
        )

    def _failed(reason: str) -> CplReference:
        logger.warning("cpl_reference: act_%s эталон не собран — %s", account, reason)
        return _result("error")

    try:
        with fb_account(offline_account_context(account)):
            response = _throttled_get(
                f"{API}/act_{get_fb_account_id()}/insights",
                params={
                    "access_token": get_fb_token(),
                    "level": "account",
                    "fields": "spend,actions",
                    "time_range": json.dumps({"since": since, "until": until}),
                },
            )
        if response.status_code != 200:
            return _failed(f"http_{response.status_code}")
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return _failed("malformed_payload")
        if not rows:
            # Кабинет ничего не тратил в окне — эталона нет, это законно.
            return _result("insufficient_leads")
        row = rows[0]
        spend = float(row.get("spend") or 0)
        parsed = parse_meta_lead_actions(row.get("actions"))
        if parsed.canonical_total is None:
            return _failed(f"invalid_lead_actions:{','.join(parsed.problems)}")
        leads = int(parsed.canonical_total)
    except Exception as exc:  # noqa: BLE001 — эталон не роняет крон
        return _failed(f"{type(exc).__name__}: {exc}"[:120])

    if leads < min_leads or spend <= 0:
        return _result("insufficient_leads", spend=spend, leads=leads)
    cpl = (Decimal(str(spend)) / Decimal(leads)).quantize(Decimal("0.01"))
    return _result("fb_account_window", spend=spend, leads=leads, cpl=cpl)


# ---------------------------------------------------------------------------
# Кэш
# ---------------------------------------------------------------------------

def _load_cache(path: Path) -> dict[str, dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_cache(path: Path, data: dict[str, dict]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("cpl_reference: кэш не сохранён — %s", exc)


def _is_fresh(entry: dict, *, now: datetime, max_age_hours: float, window_days: int) -> bool:
    if entry.get("source") not in _CACHEABLE_SOURCES:
        return False  # ошибку не кэшируем — пробуем снова
    if int(entry.get("window_days") or 0) != window_days:
        return False
    try:
        computed = datetime.fromisoformat(str(entry.get("computed_at")))
    except ValueError:
        return False
    if computed.tzinfo is None:
        computed = computed.replace(tzinfo=timezone.utc)
    return timedelta(0) <= (now - computed) <= timedelta(hours=max_age_hours)


def load_cpl_references(
    account_ids: list[str] | tuple[str, ...],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_leads: int = DEFAULT_MIN_LEADS,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    now: datetime | None = None,
    cache_path: Path | None = None,
) -> dict[str, CplReference]:
    """Эталоны по кабинетам: из кэша, если свежий, иначе один запрос FB на кабинет."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    path = cache_path or CACHE_FILE
    cache = _load_cache(path)
    result: dict[str, CplReference] = {}
    changed = False
    for raw_account in account_ids:
        account = str(raw_account).replace("act_", "")
        entry = cache.get(account)
        if isinstance(entry, dict) and _is_fresh(
            entry, now=moment, max_age_hours=max_age_hours, window_days=window_days
        ):
            try:
                result[account] = CplReference(**entry)
                continue
            except TypeError:
                pass  # старый формат кэша — пересчитываем
        ref = fetch_account_cpl_reference(
            account, window_days=window_days, min_leads=min_leads, now=moment
        )
        result[account] = ref
        if ref.source in _CACHEABLE_SOURCES:
            cache[account] = asdict(ref)
            changed = True
    if changed:
        _save_cache(path, cache)
    return result
