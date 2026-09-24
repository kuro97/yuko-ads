"""Ранний стоп: правило слива в коде — четыре класса, пауза через штатный конвейер.

Принцип правила слива: судим по расходу и заявкам, никогда по календарю; пороги
плавают вместе с кабинетом.

  * **A «ноль заявок»** (каждый час) — lifetime-расход ≥ N цен лида, заявок в AMO нет.
    Бэктест на исторической когорте объявлений: точность умеренная, лучшие
    объявления задеваются редко.
  * **B «зрелый ноль»** (каждый час) — заявки есть, среди зрелых (старше 72 ч) ни
    одного квала: ≥ b_min_mature_leads зрелых и расход ≥ max($25, 2×CPL) → пауза.
    Для иллюстрации, шанс случайного нуля при условном квале 16%: 10 заявок —
    17%, 15 — 7%, 20 — 3%. Ветка для мелких: 1–2 заявки, 0 квалов, расход ≥ 3×CPL
    (на истории — высокая точность, лучшие объявления не задеваются).
  * **C «квалы дорогие или выродились»** (раз в сутки, окно 14 дней) — ≥ 30 зрелых
    заявок в окне и (квал < 10% или цена квала > 2 когортных медиан кабинета).
    Норма квала по умолчанию — условные 16% (b_qual_norm_fallback_pct), лучшие
    держат заметно выше. День не судим: у объявления с условным квалом 25% ноль
    квалов на 5 заявках — примерно в четверти дней (0,75⁵ ≈ 24%), на 20 заявках —
    0,3%.
  * **S «голодные»** (раз в сутки) — старше s_min_age_days, расход за 14 дней меньше
    s_max_spend_usd, лидов нет, а адсет переполнен (≥ s_adset_min_active активных).
    Это не слив (денег не тратит), а слоты и размазанный бюджет: раньше такие
    объявления выключали руками пачками. Возврат — обычная пауза, не архив.

Что считается:
  * эталон CPL — скользящая цена лида кабинета по FB за 14 дней
    (services/cpl_reference.py), эталон цены квала для C — медиана по объявлениям
    с 3+ квалами в окне (fail-safe: c_cpq_fallback_usd на кабинет);
  * заявки и квалы — только точечный запрос AMO по ad_id
    (waster_rules_v2._load_lead_records); FB видит лиды, AMO нет → сбой связки,
    не паузим; квал в любой заявке выводит объявление из A/B навсегда
    (early_kill_ad_state.keep_forever), из C — нет: C смотрит окно;
  * ночь порог не меняет (заявки круглосуточно); завал ОП блокирует B и C
    (services/op_load_guard.py), A и S от него не зависят.

Режимы на правило: mode (A), b_mode, c_mode, s_mode — off | shadow | active.
  * shadow — журнал early_kill_evaluations + Telegram «поймал бы» (один раз на
    объявление и правило), мутаций нет;
  * active — предложение через action_producer_gateway.propose_pause (живой
    перечит, инвентарь адсета, «последнее активное не трогать», дедуп) +
    autonomous_pause.approve_early_kill → штатный исполнитель → кнопка «Вернуть».
    Лимит дня ограничивает только исполнение (A/B/C вместе — max_per_day, S —
    s_max_per_day); решение PAUSE в журнале не переписывается.

Никаких прямых вызовов Facebook на запись здесь нет и быть не может.
Архив после паузы — отдельная волна: транспорт мутаций запрещает ARCHIVE без
типизированного контракта владельца.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Iterable, Mapping

from services.kill_policy import (
    EvidenceStatus,
    KillCandidate,
    KillPolicyConfig,
    RolloutMode,
    SegmentTarget,
    ZeroLeadPolicy,
    evaluate_kill_policy,
)

logger = logging.getLogger(__name__)

_TZ_LOCAL = timezone(timedelta(hours=5))
_CENT = Decimal("0.01")

MODES = ("off", "shadow", "active")
MATURITY_HOURS = 72          # заявка «зрелая» — основная масса квалов известна за 3 дня (правило C и дефолт B)
AMO_TTL_MINUTES = 50         # чаще раза в час одно объявление в AMO не спрашиваем
WINDOW_DAYS = 14             # окно правила C и расхода правила S
WINDOW_TTL_HOURS = 20        # оконный срез AMO переиспользуем в пределах суток
DAILY_STATE_KEY = "early_kill_daily"

# Дефолты блока autopilot.early_kill. Зеркало обязано лежать в
# services.autopilot.AUTOPILOT_DEFAULTS["early_kill"] — get_autopilot_config
# фильтрует вложенные блоки по известным ключам.
EARLY_KILL_DEFAULTS: dict = {
    "mode": "shadow",            # правило A
    "max_age_days": 14,          # моложе N дней — кандидат для A/B
    "max_per_day": 10,           # потолок исполненных пауз в сутки (A+B+C)
    "floor_usd": 8.0,
    "cap_usd": 80.0,
    "cpl_window_days": 14,
    "cpl_min_leads": 10,
    "multiplier_no_lead": 3.0,
    "multiplier_one_lead": 4.5,
    "account_multipliers": {
        "152882611033373": {"no_lead": 4.0, "one_lead": 6.0},  # cabinet_a
    },
    # Правило B «зрелый ноль»
    "b_enabled": True,
    "b_mode": "shadow",
    "b_min_mature_leads": 3,     # боевое значение задаётся в настройках (например, 15)
    "b_maturity_hours": 72,
    # Лестница «заявки есть, квалов мало». None = прежнее поведение: одна ступень
    # (b_min_mature_leads, 0) и иммунитет после первого квала. Ступень [n, k]: зрелых ≥ n и квалов ≤ k.
    "b_ladder": None,
    "b_ladder_accounts": {},         # id кабинета → свои ступени
    "b_tail_min_leads": 25,          # с этого числа зрелых заявок судим по доле квала
    "b_tail_norm_share": None,       # порог хвоста = доля от нормы квала кабинета (0.75 → 12% при норме 16%)
    "b_qual_norm_fallback_pct": 16.0,  # норма квала, если по кабинету мало данных
    "b_qual_norm_min_leads": 100,      # минимум заявок кабинета для своей нормы квала (иначе fallback)
    "b_min_spend_usd": 25.0,
    "b_cpl_multiplier": 2.0,
    "b_small_max_leads": 2,
    "b_small_cpl_multiplier": 3.0,
    "b_exclude_name_markers": ["intl"],   # другая воронка с иным уровнем квала — B и C не применяются
    "op_guard_enabled": True,
    # Правило C «квалы дорогие/выродились» — раз в сутки
    "c_mode": "shadow",
    "c_min_mature_leads": 30,
    # Цена квала: "median" — прежнее «2 × медиана хороших при 30+ зрелых»;
    # "account_norm" — планка = c_cpq_norm_mult × средняя цена квала кабинета за окно, судим с расхода
    # c_price_min_spend_mult × планка и c_price_min_mature зрелых заявок.
    "c_price_mode": "median",
    "c_cpq_norm_mult": 1.2,
    "c_price_min_spend_mult": 2.0,
    "c_price_min_mature": 5,
    "c_cpq_norm_min_quals": 10,
    "c_cpq_norm_fallback_usd": 100.0,
    # Нижняя граница планки: норма по активным занижена (дорогие уже выключены), поэтому плавающая
    # часть может только поднять планку выше числа владельца, но не опустить.
    "c_cpq_plank_floor_usd": 120.0,
    "c_max_qual_pct": 10.0,
    "c_cpq_multiplier": 2.0,
    "c_cpq_fallback_usd": {"29716040622546856": 50.0, "152882611033373": 80.0},
    # Правило S «голодные» — раз в сутки
    "s_mode": "shadow",
    "s_min_age_days": 3,
    "s_max_spend_usd": 15.0,
    "s_adset_min_active": 15,
    "s_max_per_day": 30,
    "daily_hour": 10,            # локальный час суточного прохода C/S
}
ONE_LEAD_BOUND_SCALE = Decimal("1.5")
MIN_ADSET_ACTIVE_AFTER_S = 5   # правило S не опускает адсет ниже пяти активных

_FB_ADS_PAGE_LIMIT = 100
_FB_ADS_MAX_PAGES = 10
_FB_INSIGHTS_BATCH = 50

_RULE_TITLES = {
    "A": "ноль заявок при расходе",
    "B": "заявки есть, квалов нет",
    "C": "квалы дорогие или ниже 10% на объёме",
    "S": "голодные: без показов в переполненном адсете",
}
_REASON_CODES = {
    "A": "EARLY_KILL_SPEND_CPL",
    "B": "EARLY_KILL_MATURE_ZERO",
    "C": "EARLY_KILL_QUALITY",
    "S": "EARLY_KILL_STARVING",
}


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def early_kill_config(cfg: Mapping[str, object] | None = None) -> dict:
    """Блок autopilot.early_kill поверх дефолтов, fail-safe."""
    if cfg is None:
        try:
            from services.autopilot import get_autopilot_config

            cfg = get_autopilot_config() or {}
        except Exception as exc:  # noqa: BLE001 — настройки не роняют прогон
            logger.warning("early_kill: настройки недоступны — %s", exc)
            cfg = {}
    block = (cfg or {}).get("early_kill")
    merged = {**EARLY_KILL_DEFAULTS, **(block if isinstance(block, dict) else {})}
    for key in ("mode", "b_mode", "c_mode", "s_mode"):
        if merged.get(key) not in MODES:
            merged[key] = "off"
    return merged


def runtime_mode(cfg_block: Mapping[str, object]) -> str:
    """Режим правила A: off | shadow | active (мусор → off)."""
    mode = str(cfg_block.get("mode") or "off")
    return mode if mode in MODES else "off"


def rule_b_mode(cfg_block: Mapping[str, object]) -> str:
    if not cfg_block.get("b_enabled", True):
        return "off"
    mode = str(cfg_block.get("b_mode") or "off")
    return mode if mode in MODES else "off"


def rule_mode(cfg_block: Mapping[str, object], rule: str) -> str:
    if rule == "A":
        return runtime_mode(cfg_block)
    if rule == "B":
        return rule_b_mode(cfg_block)
    mode = str(cfg_block.get(f"{rule.lower()}_mode") or "off")
    return mode if mode in MODES else "off"


def _dec(value: object, default: Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return default
    return parsed if parsed.is_finite() and parsed > 0 else default


def _int(value: object, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def resolve_multipliers(cfg_block: Mapping[str, object], account_id: str) -> tuple[Decimal, Decimal]:
    """(без заявки, при одной заявке) для кабинета, иначе дефолтные."""
    no_lead = _dec(cfg_block.get("multiplier_no_lead"), Decimal("3.0"))
    one_lead = _dec(cfg_block.get("multiplier_one_lead"), Decimal("4.5"))
    per_account = cfg_block.get("account_multipliers")
    account = str(account_id).replace("act_", "")
    if isinstance(per_account, Mapping):
        block = per_account.get(account)
        if isinstance(block, Mapping):
            no_lead = _dec(block.get("no_lead"), no_lead)
            one_lead = _dec(block.get("one_lead"), one_lead)
    if one_lead < no_lead:
        one_lead = no_lead
    return no_lead, one_lead


def compute_threshold(
    cpl_ref: Decimal,
    multiplier: Decimal,
    *,
    floor_usd: Decimal,
    cap_usd: Decimal,
    tolerance: int = 0,
) -> Decimal:
    """Порог правила A: clamp(эталон × множитель, пол, потолок), при одной заявке границы ×1.5."""
    scale = ONE_LEAD_BOUND_SCALE if tolerance >= 1 else Decimal("1")
    low = floor_usd * scale
    high = max(cap_usd * scale, low)
    raw = cpl_ref * multiplier
    return min(max(raw, low), high).quantize(_CENT, rounding=ROUND_HALF_UP)


def is_excluded_from_rule_b(names: tuple[str, ...], cfg_block: Mapping[str, object]) -> bool:
    """Маркер из b_exclude_name_markers в имени объявления или адсета → правила B и C не применяются."""
    markers = cfg_block.get("b_exclude_name_markers") or ()
    if not isinstance(markers, (list, tuple)):
        return False
    haystack = " ".join(str(n or "") for n in names).lower()
    return any(str(m).strip().lower() in haystack for m in markers if str(m).strip())


def cpq_fallback(cfg_block: Mapping[str, object], account_id: str) -> Decimal | None:
    raw = cfg_block.get("c_cpq_fallback_usd")
    if not isinstance(raw, Mapping):
        return None
    value = raw.get(str(account_id).replace("act_", ""))
    return _dec(value, Decimal("0")) if value is not None else None


# ---------------------------------------------------------------------------
# Чистые оценки
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EarlyKillVerdict:
    decision: str                 # PAUSE | WAIT | KEEP
    reason: str
    threshold_usd: Decimal | None
    multiplier: Decimal | None
    tolerance: int                # правило A: сколько заявок «прощено» (0 или 1)


def evaluate_candidate(
    *,
    account_id: str,
    spend: Decimal | None,
    amo_leads: int | None,
    cpl_ref: Decimal | None,
    cfg_block: Mapping[str, object],
    mode: str,
) -> EarlyKillVerdict:
    """Правило A на одном объявлении. Без I/O."""
    if amo_leads is None:
        return EarlyKillVerdict("WAIT", "amo_leads_unknown", None, None, 0)
    if amo_leads >= 2:
        return EarlyKillVerdict("KEEP", "leads_present", None, None, 0)
    tolerance = int(amo_leads)
    no_lead, one_lead = resolve_multipliers(cfg_block, account_id)
    multiplier = one_lead if tolerance else no_lead
    if cpl_ref is None or cpl_ref <= 0:
        return EarlyKillVerdict("WAIT", "no_cpl_reference", None, multiplier, tolerance)

    floor_usd = _dec(cfg_block.get("floor_usd"), Decimal("8"))
    cap_usd = _dec(cfg_block.get("cap_usd"), Decimal("80"))
    threshold = compute_threshold(
        cpl_ref, multiplier, floor_usd=floor_usd, cap_usd=cap_usd, tolerance=tolerance
    )
    rollout = RolloutMode.ACTIVE if mode == "active" else (
        RolloutMode.OFF if mode == "off" else RolloutMode.SHADOW
    )
    segment = str(account_id).replace("act_", "")
    config = KillPolicyConfig(
        zero_lead_policy=ZeroLeadPolicy.SPEND_CPL,
        contour_a_rollout=rollout,
        # Множитель и пол/потолок уже применены в threshold; контуру отдаём
        # готовый порог как target_cpl × 1, чтобы деление Decimal не съело цент.
        spend_cpl_multiplier=Decimal("1"),
        targets=(SegmentTarget(segment=segment, target_cpl=threshold),),
    )
    candidate = KillCandidate(
        segment=segment,
        lifetime_evidence=EvidenceStatus.COMPLETE if spend is not None else EvidenceStatus.INCOMPLETE,
        qualification_evidence=EvidenceStatus.UNKNOWN,
        lifetime_leads=amo_leads - tolerance,
        lifetime_spend=spend,
    )
    plan = evaluate_kill_policy(config, candidate)
    contour = plan.zero_lead
    reason = contour.reason if tolerance == 0 else f"{contour.reason}:one_lead_tolerated"
    return EarlyKillVerdict(contour.decision.value.upper(), reason, threshold, multiplier, tolerance)


def ladder_steps(cfg_block: Mapping[str, object], account_id: str) -> list[tuple[int, int]]:
    """Ступени лестницы кабинета: [(зрелых ≥ n, квалов ≤ k)]. Без настройки — одна ступень «зрелый ноль»."""
    per_account = cfg_block.get("b_ladder_accounts")
    raw = per_account.get(str(account_id).replace("act_", "")) if isinstance(per_account, dict) else None
    if not raw:
        raw = cfg_block.get("b_ladder")
    steps: list[tuple[int, int]] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                continue
            try:
                n, k = int(item[0]), int(item[1])  # _int не годится: ноль квалов — законное значение
            except (TypeError, ValueError):
                continue
            if n >= 1 and 0 <= k < n:
                steps.append((n, k))
    if not steps:
        steps = [(_int(cfg_block.get("b_min_mature_leads"), 3), 0)]
    return sorted(steps)


def ladder_is_on(cfg_block: Mapping[str, object], account_id: str) -> bool:
    """Лестница судит и объявления с квалами → иммунитет «первый квал» для правила B снимается."""
    if cfg_block.get("b_tail_norm_share") is not None:
        return True
    return any(k > 0 for _, k in ladder_steps(cfg_block, account_id))


def ladder_tail_rate(cfg_block: Mapping[str, object], qual_norm: Decimal | None) -> Decimal | None:
    """Порог доли квала для хвоста: доля от плавающей нормы кабинета. None = хвост выключен."""
    share = cfg_block.get("b_tail_norm_share")
    if share is None:
        return None
    norm = qual_norm if qual_norm is not None and qual_norm > 0 else (
        _dec(cfg_block.get("b_qual_norm_fallback_pct"), Decimal("16")) / Decimal("100")
    )
    return (norm * _dec(share, Decimal("0.75"))).quantize(Decimal("0.0001"))


def ladder_hit(
    cfg_block: Mapping[str, object], account_id: str, mature: int, quals: int, qual_norm: Decimal | None
) -> str | None:
    """Код причины, если (зрелых, квалов) попадает под ступень или хвост, иначе None."""
    for n, k in ladder_steps(cfg_block, account_id):
        if mature >= n and quals <= k:
            return "mature_zero_quals" if quals == 0 else "ladder_low_quals"
    tail = ladder_tail_rate(cfg_block, qual_norm)
    if tail is not None and mature >= _int(cfg_block.get("b_tail_min_leads"), 25) and mature > 0:
        if Decimal(quals) / Decimal(mature) < tail:
            return "ladder_tail_rate"
    return None


def b_maturity_hours(cfg_block: Mapping[str, object]) -> int:
    """Срок зрелости заявки для правила B в часах, в границах 24–168."""
    return max(24, min(168, _int(cfg_block.get("b_maturity_hours"), MATURITY_HOURS)))


def evaluate_rule_b(
    *,
    account_id: str,
    age_hours: float,
    spend: Decimal | None,
    amo_leads: int | None,
    mature_leads: int | None,
    quals: int | None,
    cpl_ref: Decimal | None,
    cfg_block: Mapping[str, object],
    mode: str,
    op_ok: bool = True,
    qual_norm: Decimal | None = None,
) -> EarlyKillVerdict:
    """Правило B на одном объявлении: «зрелый ноль» и лестница «заявки есть, квалов мало». Без I/O."""
    if mode == "off" or not cfg_block.get("b_enabled", True):
        return EarlyKillVerdict("WAIT", "contour_off", None, None, 0)
    maturity_hours = b_maturity_hours(cfg_block)
    if age_hours < maturity_hours:
        return EarlyKillVerdict("WAIT", f"age_below_{maturity_hours}h", None, None, 0)
    if amo_leads is None or mature_leads is None or quals is None:
        return EarlyKillVerdict("WAIT", "amo_leads_unknown", None, None, 0)
    if amo_leads == 0:
        return EarlyKillVerdict("WAIT", "no_leads_rule_a", None, None, 0)
    hit = ladder_hit(cfg_block, account_id, mature_leads, quals, qual_norm)
    if quals >= 1 and hit is None:
        return EarlyKillVerdict("KEEP", "quals_present", None, None, 0)
    if spend is None:
        return EarlyKillVerdict("WAIT", "insufficient_lifetime_evidence", None, None, 0)

    min_spend = _dec(cfg_block.get("b_min_spend_usd"), Decimal("25"))
    mult = _dec(cfg_block.get("b_cpl_multiplier"), Decimal("2"))
    small_max = _int(cfg_block.get("b_small_max_leads"), 2)
    small_mult = _dec(cfg_block.get("b_small_cpl_multiplier"), Decimal("3"))
    floor_usd = _dec(cfg_block.get("floor_usd"), Decimal("8"))
    cap_usd = _dec(cfg_block.get("cap_usd"), Decimal("80"))

    def _pause_or_guard(reason: str, threshold: Decimal, multiplier: Decimal) -> EarlyKillVerdict:
        if not op_ok:
            return EarlyKillVerdict("WAIT", "op_load_guard", threshold, multiplier, 0)
        return EarlyKillVerdict("PAUSE", reason, threshold, multiplier, 0)

    if hit is not None:
        threshold = min_spend
        if cpl_ref is not None and cpl_ref > 0:
            threshold = max(min_spend, (cpl_ref * mult).quantize(_CENT, rounding=ROUND_HALF_UP))
        if spend >= threshold:
            return _pause_or_guard(hit, threshold, mult)
        return EarlyKillVerdict("WAIT", "spend_below_threshold", threshold, mult, 0)
    if 1 <= amo_leads <= small_max:
        if cpl_ref is None or cpl_ref <= 0:
            return EarlyKillVerdict("WAIT", "no_cpl_reference", None, small_mult, 0)
        threshold = compute_threshold(
            cpl_ref, small_mult, floor_usd=floor_usd, cap_usd=cap_usd, tolerance=1
        )
        if spend >= threshold:
            return _pause_or_guard("small_leads_zero_quals", threshold, small_mult)
        return EarlyKillVerdict("WAIT", "spend_below_threshold", threshold, small_mult, 0)
    return EarlyKillVerdict("WAIT", "mature_below_min", None, mult, 0)


def evaluate_rule_c(
    *,
    spend_14d: Decimal | None,
    leads_14d: int | None,
    mature_14d: int | None,
    quals_14d: int | None,
    cpq_ref: Decimal | None,
    cfg_block: Mapping[str, object],
    mode: str,
    op_ok: bool = True,
    account_id: str = "",
    qual_norm: Decimal | None = None,
) -> EarlyKillVerdict:
    """Правило C «квалы дорогие или выродились» на окне 14 дней. Без I/O.

    При включённой лестнице качество окна судит она (те же ступени и хвост, что у B), цена квала —
    по-прежнему с c_min_mature_leads зрелых заявок.

    ≥ c_min_mature_leads зрелых заявок в окне и (квал < c_max_qual_pct или цена
    квала > c_cpq_multiplier × эталон) → PAUSE. Ноль квалов на таком объёме —
    тоже «квал ниже 10%». Страж ОП переводит PAUSE в WAIT.
    """
    if mode == "off":
        return EarlyKillVerdict("WAIT", "contour_off", None, None, 0)
    if leads_14d is None or mature_14d is None or quals_14d is None:
        return EarlyKillVerdict("WAIT", "amo_leads_unknown", None, None, 0)
    if spend_14d is None:
        return EarlyKillVerdict("WAIT", "insufficient_window_evidence", None, None, 0)
    min_mature = _int(cfg_block.get("c_min_mature_leads"), 30)
    mult = _dec(cfg_block.get("c_cpq_multiplier"), Decimal("2"))
    if ladder_is_on(cfg_block, account_id):
        hit = ladder_hit(cfg_block, account_id, mature_14d, quals_14d, qual_norm)
        if hit is not None and spend_14d >= _dec(cfg_block.get("b_min_spend_usd"), Decimal("25")):
            if not op_ok:
                return EarlyKillVerdict("WAIT", "op_load_guard", None, mult, 0)
            return EarlyKillVerdict("PAUSE", hit, None, mult, 0)
    if cfg_block.get("c_price_mode") == "account_norm":
        # cpq_ref здесь — средняя цена квала кабинета; планка плавает вместе с ней
        norm = cpq_ref if cpq_ref is not None and cpq_ref > 0 else _dec(
            cfg_block.get("c_cpq_norm_fallback_usd"), Decimal("100"))
        plank_mult = _dec(cfg_block.get("c_cpq_norm_mult"), Decimal("1.2"))
        plank = max(
            (norm * plank_mult).quantize(_CENT, rounding=ROUND_HALF_UP),
            _dec(cfg_block.get("c_cpq_plank_floor_usd"), Decimal("120")).quantize(_CENT),
        )
        judge_from = plank * _dec(cfg_block.get("c_price_min_spend_mult"), Decimal("2"))
        if mature_14d < _int(cfg_block.get("c_price_min_mature"), 5):
            return EarlyKillVerdict("WAIT", "mature_below_min", plank, plank_mult, 0)
        if spend_14d < judge_from:
            return EarlyKillVerdict("WAIT", "spend_below_threshold", plank, plank_mult, 0)
        if quals_14d > 0 and spend_14d / Decimal(quals_14d) <= plank:
            return EarlyKillVerdict("KEEP", "quality_ok", plank, plank_mult, 0)
        if not op_ok:
            return EarlyKillVerdict("WAIT", "op_load_guard", plank, plank_mult, 0)
        return EarlyKillVerdict("PAUSE", "quality_expensive", plank, plank_mult, 0)
    if mature_14d < min_mature:
        return EarlyKillVerdict("WAIT", "mature_below_min", None, None, 0)
    max_qual = _dec(cfg_block.get("c_max_qual_pct"), Decimal("10")) / Decimal("100")
    qual_rate = Decimal(quals_14d) / Decimal(leads_14d) if leads_14d else Decimal("0")
    if qual_rate < max_qual:
        verdict = EarlyKillVerdict("PAUSE", "quality_low", None, mult, 0)
    elif cpq_ref is not None and cpq_ref > 0 and quals_14d > 0:
        threshold = (cpq_ref * mult).quantize(_CENT, rounding=ROUND_HALF_UP)
        cpq = spend_14d / Decimal(quals_14d)
        if cpq > threshold:
            verdict = EarlyKillVerdict("PAUSE", "quality_expensive", threshold, mult, 0)
        else:
            return EarlyKillVerdict("KEEP", "quality_ok", threshold, mult, 0)
    else:
        return EarlyKillVerdict("KEEP", "quality_ok", None, mult, 0)
    if not op_ok:
        return EarlyKillVerdict("WAIT", "op_load_guard", verdict.threshold_usd, mult, 0)
    return verdict


def evaluate_rule_s(
    *,
    age_hours: float,
    spend_14d: Decimal | None,
    fb_leads: int | None,
    adset_active: int,
    cfg_block: Mapping[str, object],
    mode: str,
) -> EarlyKillVerdict:
    """Правило S «голодные»: старше s_min_age_days, расход за окно < s_max_spend_usd,
    лидов по FB нет, адсет переполнен (≥ s_adset_min_active активных) → PAUSE. Без I/O."""
    if mode == "off":
        return EarlyKillVerdict("WAIT", "contour_off", None, None, 0)
    if spend_14d is None or fb_leads is None:
        return EarlyKillVerdict("WAIT", "insufficient_window_evidence", None, None, 0)
    min_age_h = _int(cfg_block.get("s_min_age_days"), 3) * 24
    if age_hours <= min_age_h:
        return EarlyKillVerdict("WAIT", "too_young", None, None, 0)
    if fb_leads >= 1:
        return EarlyKillVerdict("KEEP", "leads_present", None, None, 0)
    max_spend = _dec(cfg_block.get("s_max_spend_usd"), Decimal("15"))
    if spend_14d >= max_spend:
        return EarlyKillVerdict("KEEP", "spending", max_spend, None, 0)
    if adset_active < _int(cfg_block.get("s_adset_min_active"), 15):
        return EarlyKillVerdict("WAIT", "adset_not_crowded", max_spend, None, 0)
    return EarlyKillVerdict("PAUSE", "starving", max_spend, None, 0)


# ---------------------------------------------------------------------------
# Живые данные
# ---------------------------------------------------------------------------

def _parse_created(created_time: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(created_time).replace("+0000", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _fetch_young_active_ads(account_id: str, *, now: datetime, max_age_days: int | None) -> list[dict]:
    """ACTIVE объявления кабинета (моложе max_age_days, None = все): {ad_id, name, adset_id, adset_name, created, age_hours}.

    Один кабинет за вызов, вызывающий оборачивает в fb_account(...). Бросает при
    отказе FB — прогон помечает кабинет недоступным и идёт дальше.
    """
    from agent.fb_common import API, FBApiError, _throttled_get
    from services.fb_token_provider import get_fb_account_id, get_fb_token

    filters: list[dict] = [{"field": "effective_status", "operator": "IN", "value": ["ACTIVE"]}]
    if max_age_days is not None:
        cutoff_ts = int((now - timedelta(days=max_age_days)).timestamp())
        filters.insert(0, {"field": "created_time", "operator": "GREATER_THAN", "value": cutoff_ts})
    result: list[dict] = []
    cursor: str | None = None
    for _page in range(_FB_ADS_MAX_PAGES):
        params: dict = {
            "access_token": get_fb_token(),
            "fields": "id,name,adset_id,adset{name},created_time,effective_status",
            "filtering": json.dumps(filters),
            "limit": _FB_ADS_PAGE_LIMIT,
        }
        if cursor:
            params["after"] = cursor
        resp = _throttled_get(f"{API}/act_{get_fb_account_id()}/ads", params=params)
        if resp.status_code != 200:
            raise FBApiError(f"FB /ads ошибка: {resp.status_code} {resp.text[:200]}", resp.status_code)
        data = resp.json()
        for ad in data.get("data", []):
            created = _parse_created(ad.get("created_time"))
            if not ad.get("id") or created is None:
                continue
            age_hours = (now - created).total_seconds() / 3600
            if ad.get("effective_status") != "ACTIVE":
                continue
            if max_age_days is not None and age_hours >= max_age_days * 24:
                continue  # серверный фильтр перепроверяем локально
            result.append({
                "ad_id": str(ad["id"]),
                "name": str(ad.get("name") or ""),
                "adset_id": str(ad.get("adset_id") or ""),
                "adset_name": str((ad.get("adset") or {}).get("name") or ""),
                "created": created,
                "age_hours": round(age_hours, 1),
            })
        paging = data.get("paging", {})
        cursor = paging.get("cursors", {}).get("after")
        if "next" not in paging or not cursor:
            break
    return result


def _fetch_insights_batches(ad_ids: list[str], extra_params: dict) -> dict[str, dict | None]:
    """Общий батч-запрос insights level=ad по списку id: spend/impressions/FB-лиды.

    Отсутствие строки = нули (объявление не крутилось), провал батча → None по всем его id.
    """
    from agent.fb_common import API, _throttled_get
    from services.fb_token_provider import get_fb_account_id, get_fb_token
    from services.meta_lead_actions import parse_meta_lead_actions

    result: dict[str, dict | None] = {}
    for start in range(0, len(ad_ids), _FB_INSIGHTS_BATCH):
        batch = ad_ids[start:start + _FB_INSIGHTS_BATCH]
        try:
            resp = _throttled_get(
                f"{API}/act_{get_fb_account_id()}/insights",
                params={
                    "access_token": get_fb_token(),
                    "level": "ad",
                    "fields": "ad_id,spend,impressions,actions",
                    "filtering": json.dumps([{"field": "ad.id", "operator": "IN", "value": batch}]),
                    "limit": len(batch) + 1,  # +1: полная страница не должна прийти с курсором next
                    **extra_params,
                },
            )
            if resp.status_code != 200:
                raise ValueError(f"http_{resp.status_code}: {resp.text[:120]}")
            payload = resp.json()
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ValueError("malformed_payload")
            paging = payload.get("paging")
            if isinstance(paging, dict) and paging.get("next"):
                raise ValueError("incomplete_paging")
            parsed: dict[str, dict] = {ad_id: {"spend": 0.0, "impressions": 0, "fb_leads": 0} for ad_id in batch}
            for row in rows:
                ad_id = str(row.get("ad_id") or "")
                if ad_id not in parsed:
                    raise ValueError(f"unrequested_ad_id:{ad_id}")
                leads = parse_meta_lead_actions(row.get("actions"))
                if leads.canonical_total is None:
                    raise ValueError(f"invalid_lead_actions:{ad_id}:{','.join(leads.problems)}")
                parsed[ad_id] = {
                    "spend": float(row.get("spend") or 0),
                    "impressions": int(row.get("impressions") or 0),
                    "fb_leads": int(leads.canonical_total),
                }
            result.update(parsed)
        except Exception as exc:  # noqa: BLE001 — неполное evidence, не пауза
            logger.warning(
                "early_kill: батч insights из %d объявлений не собран (%s: %s)",
                len(batch), type(exc).__name__, str(exc)[:160],
            )
            for ad_id in batch:
                result[ad_id] = None
    return result


def _fetch_live_lifetime(ad_ids: list[str]) -> dict[str, dict | None]:
    """Lifetime spend/impressions/FB-лиды батчами ≤50 в текущем fb_account."""
    return _fetch_insights_batches(ad_ids, {"date_preset": "maximum"})


def _fetch_window_stats(ad_ids: list[str], *, now: datetime) -> dict[str, dict | None]:
    """Spend/FB-лиды за окно WINDOW_DAYS (полные дни по локальному времени до вчера включительно)."""
    today = now.astimezone(_TZ_LOCAL).date()
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    until = (today - timedelta(days=1)).isoformat()
    return _fetch_insights_batches(ad_ids, {"time_range": json.dumps({"since": since, "until": until})})


def _fetch_amo_summary(
    ad_id: str, created: datetime, *, now: datetime, b_hours: int = MATURITY_HOURS
) -> dict | None:
    """Заявки AMO по объявлению с даты запуска: lifetime и окно 14 дней. None = AMO недоступен.

    Зрелость lifetime-заявок (правило B) считается по b_hours, окна 14 дней (правило C) — по MATURITY_HOURS.
    """
    from services.waster_rules_v2 import _load_lead_records

    try:
        records = _load_lead_records(ad_id, created.astimezone(_TZ_LOCAL).date())
    except Exception as exc:  # noqa: BLE001 — недоказанное не режется
        logger.warning("early_kill: AMO по %s недоступен — %s", ad_id, str(exc)[:80])
        return None
    mature_cutoff = now - timedelta(hours=MATURITY_HOURS)
    b_cutoff = now - timedelta(hours=b_hours)
    window_from = now - timedelta(days=WINDOW_DAYS)
    recs = [(m.astimezone(timezone.utc), q) for m, q in records]
    window = [(m, q) for m, q in recs if m >= window_from]
    return {
        "leads": len(recs),
        "mature": sum(1 for m, _ in recs if m <= b_cutoff),
        "quals": sum(1 for _, q in recs if q),
        "leads_14d": len(window),
        "mature_14d": sum(1 for m, _ in window if m <= mature_cutoff),
        "quals_14d": sum(1 for _, q in window if q),
    }


# ---------------------------------------------------------------------------
# Состояние по объявлению и журнал оценок (decisions.db)
# ---------------------------------------------------------------------------

def _load_ad_states(ad_ids: list[str]) -> dict[str, dict]:
    if not ad_ids:
        return {}
    from services.creative_intelligence import _get_connection

    conn = _get_connection()
    try:
        marks = ",".join("?" for _ in ad_ids)
        rows = conn.execute(
            f"SELECT ad_id, keep_forever, first_qual_seen_at, last_amo_at, amo_leads, amo_mature_leads, amo_quals, "
            f"amo_leads_14d, amo_mature_14d, amo_quals_14d, window_at "
            f"FROM early_kill_ad_state WHERE ad_id IN ({marks})",
            ad_ids,
        ).fetchall()
        return {str(row["ad_id"]): dict(row) for row in rows}
    finally:
        conn.close()


def _save_ad_state(ad: dict, account_id: str, summary: dict, *, now: datetime, previous: dict | None) -> None:
    """UPSERT состояния после ответа AMO. keep_forever ставится навсегда при первом квале."""
    from services.creative_intelligence import _get_connection

    had_qual = bool(previous and previous.get("keep_forever"))
    keep = 1 if (had_qual or summary["quals"] >= 1) else 0
    first_qual = (previous or {}).get("first_qual_seen_at") or (now.isoformat() if summary["quals"] >= 1 else None)
    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO early_kill_ad_state (
                ad_id, account_id, created_time, keep_forever, first_qual_seen_at,
                last_amo_at, amo_leads, amo_mature_leads, amo_quals,
                amo_leads_14d, amo_mature_14d, amo_quals_14d, window_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ad_id) DO UPDATE SET
                keep_forever = excluded.keep_forever,
                first_qual_seen_at = COALESCE(early_kill_ad_state.first_qual_seen_at, excluded.first_qual_seen_at),
                last_amo_at = excluded.last_amo_at,
                amo_leads = excluded.amo_leads,
                amo_mature_leads = excluded.amo_mature_leads,
                amo_quals = excluded.amo_quals,
                amo_leads_14d = excluded.amo_leads_14d,
                amo_mature_14d = excluded.amo_mature_14d,
                amo_quals_14d = excluded.amo_quals_14d,
                window_at = excluded.window_at,
                updated_at = excluded.updated_at
            """,
            (
                ad["ad_id"], account_id, ad["created"].isoformat(), keep, first_qual,
                now.isoformat(), summary["leads"], summary["mature"], summary["quals"],
                summary.get("leads_14d"), summary.get("mature_14d"), summary.get("quals_14d"),
                now.isoformat(), now.isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _record_evaluations(rows: list[dict]) -> None:
    if not rows:
        return
    from services.creative_intelligence import _get_connection

    conn = _get_connection()
    try:
        conn.executemany(
            """
            INSERT INTO early_kill_evaluations (
                run_id, evaluated_at, mode, account_id, ad_id, ad_name, adset_id,
                created_time, age_hours, spend_usd, impressions, fb_leads, amo_leads,
                cpl_ref_usd, multiplier, threshold_usd, decision, reason,
                rule, mature_leads, quals, action, proposal_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["run_id"], r["evaluated_at"], r["mode"], r["account_id"], r["ad_id"],
                    r.get("ad_name"), r.get("adset_id"), r.get("created_time"), r.get("age_hours"),
                    r.get("spend_usd"), r.get("impressions"), r.get("fb_leads"), r.get("amo_leads"),
                    r.get("cpl_ref_usd"), r.get("multiplier"), r.get("threshold_usd"),
                    r["decision"], r["reason"], r["rule"], r.get("mature_leads"), r.get("quals"),
                    r.get("action"), r.get("proposal_id"),
                )
                for r in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _previously_flagged(pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """(rule, ad_id), по которым PAUSE уже был записан раньше (дедуп «поймал бы»)."""
    if not pairs:
        return set()
    from services.creative_intelligence import _get_connection

    ad_ids = sorted({ad_id for _, ad_id in pairs})
    conn = _get_connection()
    try:
        marks = ",".join("?" for _ in ad_ids)
        rows = conn.execute(
            f"SELECT DISTINCT rule, ad_id FROM early_kill_evaluations WHERE decision = 'PAUSE' AND ad_id IN ({marks})",
            ad_ids,
        ).fetchall()
        return {(str(row[0]), str(row[1])) for row in rows}
    finally:
        conn.close()


def _previously_approved(ad_ids: list[str]) -> set[str]:
    """Объявления, по которым предложение уже создавалось и самоодобрялось (любым правилом)."""
    if not ad_ids:
        return set()
    from services.creative_intelligence import _get_connection

    conn = _get_connection()
    try:
        marks = ",".join("?" for _ in ad_ids)
        rows = conn.execute(
            f"SELECT DISTINCT ad_id FROM early_kill_evaluations WHERE action = 'approved' AND ad_id IN ({marks})",
            ad_ids,
        ).fetchall()
        return {str(row[0]) for row in rows}
    finally:
        conn.close()


def _approved_today(now: datetime, rules: tuple[str, ...]) -> int:
    """Сколько разных объявлений уже исполнено сегодня (локальное время) указанными правилами."""
    from services.creative_intelligence import _get_connection

    day_start = now.astimezone(_TZ_LOCAL).replace(hour=0, minute=0, second=0, microsecond=0)
    conn = _get_connection()
    try:
        marks = ",".join("?" for _ in rules)
        row = conn.execute(
            f"SELECT COUNT(DISTINCT ad_id) FROM early_kill_evaluations "
            f"WHERE action = 'approved' AND evaluated_at >= ? AND rule IN ({marks})",
            (day_start.astimezone(timezone.utc).isoformat(), *rules),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _daily_state() -> dict:
    from services.creative_backfill import _get_state_by_key

    return _get_state_by_key(DAILY_STATE_KEY)


def _save_daily_state(state: dict) -> None:
    from services.creative_backfill import _save_state_by_key

    _save_state_by_key(DAILY_STATE_KEY, state)


# ---------------------------------------------------------------------------
# Исполнение (mode=active): предложение + самоодобрение
# ---------------------------------------------------------------------------

def _business_reason(row: dict) -> str:
    rule = row["rule"]
    age = float(row.get("age_hours") or 0)
    if rule == "A":
        return (
            f"ранний стоп: {_money(row.get('spend_usd'))} за {age:.0f} ч, заявок в AMO {row.get('amo_leads')}, "
            f"порог {_money(row.get('threshold_usd'))} (цена лида {_money(row.get('cpl_ref_usd'))} × {row.get('multiplier')})"
        )
    if rule == "B":
        return (
            f"заявки без квалов: {_money(row.get('spend_usd'))}, заявок {row.get('amo_leads')} "
            f"(зрелых {row.get('mature_leads')}), квалов {row.get('quals') or 0}, порог {_money(row.get('threshold_usd'))}"
        )
    if rule == "C":
        return (
            f"качество за 14 дн: {_money(row.get('spend_usd'))}, заявок {row.get('amo_leads')} "
            f"(зрелых {row.get('mature_leads')}), квалов {row.get('quals')} — "
            + (f"цена квала выше {_money(row.get('threshold_usd'))}" if row.get("reason") == "quality_expensive"
               else "квалов меньше, чем положено на столько заявок")
        )
    return (
        f"голодное: {age/24:.0f} дн, расход за 14 дн {_money(row.get('spend_usd'))}, лидов нет, "
        f"адсет переполнен"
    )


def _execute_rules(rows: list[dict], *, cfg: Mapping[str, object], now: datetime, report: dict,
                   adset_active: Mapping[str, int] | None = None) -> list[dict]:
    """Создаёт предложения паузы и самоодобряет их. Возвращает исполненные строки.

    Прямой мутации нет: propose_pause делает живой перечит (ACTIVE, инвентарь
    адсета, «последнее активное не трогать»), самоодобрение ставит задание в
    очередь, паузу физически делает исполнитель конвейера. Лимиты дня и дедуп —
    здесь, у строк меняется только action. Правило S не опускает адсет ниже
    MIN_ADSET_ACTIVE_AFTER_S и считает свой лимит s_max_per_day.
    """
    from services import autonomous_pause as autonomous
    from services.action_producer_gateway import ProducerActionError, propose_pause
    from services.owner_proposal_card import DecisionContext

    max_per_day = _int(cfg.get("max_per_day"), 10)
    s_max_per_day = _int(cfg.get("s_max_per_day"), 30)
    approved_before = _previously_approved([r["ad_id"] for r in rows])
    used_main = _approved_today(now, ("A", "B", "C"))
    used_s = _approved_today(now, ("S",))
    remaining_adset: dict[str, int] = dict(adset_active or {})
    executed: list[dict] = []
    done_ads: set[str] = set()
    for row in sorted(rows, key=lambda r: (r["rule"] == "S", -float(r.get("spend_usd") or 0))):
        ad_id = row["ad_id"]
        rule = row["rule"]
        if ad_id in approved_before or ad_id in done_ads:
            row["action"] = "dedup"
            continue
        if rule == "S":
            if used_s >= s_max_per_day:
                row["action"] = "capped"
                continue
            left = remaining_adset.get(row.get("adset_id") or "", MIN_ADSET_ACTIVE_AFTER_S + 1)
            if left - 1 < MIN_ADSET_ACTIVE_AFTER_S:
                row["action"] = "adset_min"
                continue
        elif used_main >= max_per_day:
            row["action"] = "capped"
            continue
        business_reason = _business_reason(row)
        try:
            outcome = propose_pause(
                ad_id,
                origin="AUTOPILOT_EARLY_KILL",
                scope=f"early-kill:{rule}:{ad_id}",
                reason_code=_REASON_CODES[rule],
                decision=DecisionContext(
                    spend_usd=row.get("spend_usd"),
                    leads=row.get("amo_leads") if row.get("amo_leads") is not None else row.get("fb_leads"),
                    cpl_usd=row.get("cpl_ref_usd"),
                    qual_leads=row.get("quals"),
                    days_running=int(float(row.get("age_hours") or 0) // 24),
                    business_reason=business_reason,
                ),
                now=now,
            )
        except ProducerActionError as exc:
            row["action"] = f"error:{str(exc)[:48]}"
            logger.info("early_kill: гейтвей отказал по %s — %s", ad_id, exc)
            continue
        except Exception as exc:  # noqa: BLE001 — одна реклама не роняет прогон
            row["action"] = f"error:{type(exc).__name__}"
            logger.warning("early_kill: предложение по %s не создано — %s", ad_id, exc)
            continue
        receipt = outcome.receipt
        if receipt is None:
            row["action"] = f"error:{outcome.reason or outcome.action}"
            continue
        row["proposal_id"] = receipt.proposal_id
        if receipt.deduplicated:
            row["action"] = "dedup"
            continue
        approved = autonomous.approve_early_kill(
            receipt.proposal_id,
            rule=rule,
            evidence={
                "business_reason": business_reason, "rule": rule,
                "spend_usd": row.get("spend_usd"), "amo_leads": row.get("amo_leads"),
                "mature_leads": row.get("mature_leads"), "quals": row.get("quals"),
                "cpl_ref_usd": row.get("cpl_ref_usd"), "threshold_usd": row.get("threshold_usd"),
                "age_hours": row.get("age_hours"),
            },
            now=now,
        )
        if not approved:
            row["action"] = "proposed_only"
            continue
        row["action"] = "approved"
        executed.append(row)
        done_ads.add(ad_id)
        if rule == "S":
            used_s += 1
            key = row.get("adset_id") or ""
            remaining_adset[key] = remaining_adset.get(key, MIN_ADSET_ACTIVE_AFTER_S + 1) - 1
        else:
            used_main += 1
        try:
            autonomous.record_autonomous_pause(
                {
                    "ad_id": ad_id, "id": ad_id, "name": row.get("ad_name") or ad_id,
                    "spend": row.get("spend_usd"), "leads": row.get("amo_leads"),
                    "cpl": row.get("cpl_ref_usd"), "reason": business_reason, "rule": rule,
                },
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 — журнал сводки не роняет прогон
            logger.warning("early_kill: журнал автономных пауз не записан — %s", exc)
    report["approved"] = len(executed)
    report["capped"] = sum(1 for r in rows if r.get("action") == "capped")
    return executed


def _active_message(items: list[dict]) -> str:
    lines = [f"⏱ Ранний стоп: выключаю {len(items)} объявл."]
    for rule in ("A", "B", "C", "S"):
        group = [i for i in items if i["rule"] == rule]
        if not group:
            continue
        lines.append(f"\n{rule} — {_RULE_TITLES[rule]}:")
        for item in group[:10]:
            lines.append(
                f"• {str(item.get('ad_name') or '')[:60]} — {_money(item.get('spend_usd'))} за "
                f"{float(item.get('age_hours') or 0):.0f} ч, заявок {item.get('amo_leads') if item.get('amo_leads') is not None else item.get('fb_leads')}, "
                f"квалов {item.get('quals') if item.get('quals') is not None else '—'}"
            )
        if len(group) > 10:
            lines.append(f"… и ещё {len(group) - 10}")
    lines.append("\nПауза исполнится конвейером в ближайшие полчаса. Не согласны — «↩️ Вернуть».")
    return "\n".join(lines)


def _notify_executed(items: list[dict]) -> None:
    if not items:
        return
    try:
        from services.autopilot import _build_pause_undo_buttons
        from services.telegram_bot import send_with_buttons

        buttons = _build_pause_undo_buttons(
            [{"id": str(i["ad_id"]), "name": str(i.get("ad_name") or "")} for i in items]
        )
        send_with_buttons(_active_message(items), buttons)
    except Exception as exc:  # noqa: BLE001 — сообщение не роняет прогон
        logger.warning("early_kill: сообщение о паузах не ушло — %s", exc)


# ---------------------------------------------------------------------------
# Прогон
# ---------------------------------------------------------------------------

def _money(value: Decimal | float | None) -> str:
    if value is None:
        return "—"
    return f"${Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)}"


def _shadow_message(items: list[dict]) -> str:
    lines = [f"⏱ Ранний стоп (тень): поймал бы {len(items)} объявл."]
    for rule in ("A", "B", "C", "S"):
        group = [item for item in items if item["rule"] == rule]
        if not group:
            continue
        lines.append(f"\n{rule} — {_RULE_TITLES[rule]}:")
        for item in group[:10]:
            if rule == "A":
                extra = f"заявок AMO: {item['amo_leads']}"
            elif rule == "S":
                extra = "лидов нет"
            else:
                extra = f"заявок {item['amo_leads']} (зрелых {item['mature_leads']}), квалов {item.get('quals')}"
            lines.append(
                f"• {item['ad_name'][:60]} — {_money(item['spend_usd'])} за {float(item['age_hours']):.0f} ч, "
                f"{extra}, порог {_money(item.get('threshold_usd'))}"
            )
        if len(group) > 10:
            lines.append(f"… и ещё {len(group) - 10}")
    lines.append("\nНичего не выключено: режим наблюдения.")
    return "\n".join(lines)


def run_early_kill(*, now: datetime | None = None, trigger: str = "cron") -> dict:
    """Один прогон правил по всем кабинетам карты. Никогда не бросает."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    run_id = uuid.uuid4().hex[:12]
    report: dict = {
        "ran": False, "skipped": None, "run_id": run_id, "trigger": trigger,
        "mode": None, "b_mode": None, "c_mode": None, "s_mode": None, "daily": False,
        "accounts": [], "failed_accounts": [], "candidates": 0, "evaluated": 0,
        "amo_queries": 0, "amo_cached": 0, "would_pause": [], "would_pause_a": [], "would_pause_b": [],
        "would_pause_c": [], "would_pause_s": [], "cpq_ref": {}, "qual_norm": {},
        "approved": 0, "capped": 0, "executed": [], "op_guard": None, "recorded": 0, "errors": [],
    }
    try:
        return _run_inner(moment, run_id, report)
    except Exception as exc:  # noqa: BLE001 — крон не падает
        logger.exception("early_kill: неожиданная ошибка прогона %s", run_id)
        report["errors"].append(f"{type(exc).__name__}: {exc}"[:200])
        return report


def _account_qual_norm(states: Iterable[dict], cfg: Mapping[str, object]) -> Decimal | None:
    """Плавающая норма квала кабинета: квалы / заявки за окно 14 дней по кэшу активных объявлений.

    None при малой выборке — тогда хвост лестницы берёт b_qual_norm_fallback_pct.
    """
    leads = quals = 0
    for state in states:
        if not state or state.get("amo_leads_14d") is None:
            continue
        leads += int(state.get("amo_leads_14d") or 0)
        quals += int(state.get("amo_quals_14d") or 0)
    if leads < _int(cfg.get("b_qual_norm_min_leads"), 100):
        return None
    return (Decimal(quals) / Decimal(leads)).quantize(Decimal("0.0001"))


def _amo_is_fresh(state: dict | None, now: datetime, *, key: str = "last_amo_at", ttl: timedelta | None = None) -> bool:
    if not state or not state.get(key):
        return False
    try:
        last = datetime.fromisoformat(str(state[key]))
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) < (ttl or timedelta(minutes=AMO_TTL_MINUTES))


def _is_daily_slot(now: datetime, cfg: Mapping[str, object], *, forced: bool = False) -> bool:
    """Суточный проход C/S: час daily_hour по локальному времени, один раз в день."""
    if forced:
        return True
    local = now.astimezone(_TZ_LOCAL)
    if local.hour != int(cfg.get("daily_hour", 10)):
        return False
    try:
        state = _daily_state()
    except Exception as exc:  # noqa: BLE001 — без state считаем, что сегодня ещё не бежали
        logger.warning("early_kill: суточный state недоступен — %s", exc)
        state = {}
    return state.get("last_date") != local.date().isoformat()


def _run_inner(moment: datetime, run_id: str, report: dict) -> dict:
    from services.autopilot import get_autopilot_config
    from services.cpl_reference import load_cpl_references
    from services.fb_token_provider import fb_account, offline_account_context
    from services.launch_routing import accounts_to_scan

    autopilot_cfg = get_autopilot_config() or {}
    cfg = early_kill_config(autopilot_cfg)
    modes = {rule: rule_mode(cfg, rule) for rule in ("A", "B", "C", "S")}
    report.update({"mode": modes["A"], "b_mode": modes["B"], "c_mode": modes["C"], "s_mode": modes["S"]})
    if all(m == "off" for m in modes.values()):
        report["skipped"] = "mode_off"
        return report
    if autopilot_cfg.get("kill_switch") is True:
        report["skipped"] = "kill_switch"
        return report

    daily = (modes["C"] != "off" or modes["S"] != "off") and _is_daily_slot(
        moment, cfg, forced=report.get("trigger") == "daily"
    )
    report["daily"] = daily
    if daily:
        try:
            _save_daily_state({"last_date": moment.astimezone(_TZ_LOCAL).date().isoformat(), "run_id": run_id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("early_kill: суточный state не сохранён — %s", exc)

    max_age_days = _int(cfg.get("max_age_days"), 14)
    accounts = [str(a).replace("act_", "") for a in accounts_to_scan()]
    report["accounts"] = accounts
    if not accounts:
        report["skipped"] = "no_accounts"
        return report

    refs = load_cpl_references(
        accounts,
        window_days=_int(cfg.get("cpl_window_days"), 14),
        min_leads=_int(cfg.get("cpl_min_leads"), 10),
        now=moment,
    )

    # Страж загрузки ОП: один замер на прогон, блокирует правила B и C.
    op_ok = True
    if (modes["B"] != "off" or (daily and modes["C"] != "off")) and cfg.get("op_guard_enabled", True):
        from services.op_load_guard import check_op_load

        verdict = check_op_load(now=moment)
        op_ok = verdict.ok
        report["op_guard"] = {
            "ok": verdict.ok, "reason": verdict.reason, "share": verdict.share,
            "baseline": verdict.baseline, "leads": verdict.leads,
        }

    floor_usd = _dec(cfg.get("floor_usd"), Decimal("8"))
    cap_usd = _dec(cfg.get("cap_usd"), Decimal("80"))
    c_min_mature = _int(cfg.get("c_min_mature_leads"), 30)
    b_hours = b_maturity_hours(cfg)
    evaluations: list[dict] = []
    adset_active_counts: dict[str, int] = {}
    for account in accounts:
        try:
            with fb_account(offline_account_context(account)):
                ads = _fetch_young_active_ads(account, now=moment, max_age_days=None if daily else max_age_days)
                ids = [ad["ad_id"] for ad in ads]
                lifetime = _fetch_live_lifetime(ids) if ids else {}
                window = _fetch_window_stats(ids, now=moment) if (daily and ids) else {}
        except Exception as exc:  # noqa: BLE001 — кабинет недоступен, остальные идут
            logger.warning("early_kill: кабинет act_%s недоступен (%s)", account, type(exc).__name__)
            report["failed_accounts"].append(account)
            continue
        report["candidates"] += len(ads)
        for ad in ads:
            adset_active_counts[ad["adset_id"]] = adset_active_counts.get(ad["adset_id"], 0) + 1
        ref = refs.get(account)
        cpl_ref = ref.cpl if ref is not None else None
        no_lead_mult, _ = resolve_multipliers(cfg, account)
        prescreen_a = (
            compute_threshold(cpl_ref, no_lead_mult, floor_usd=floor_usd, cap_usd=cap_usd)
            if cpl_ref else None
        )
        states = _load_ad_states(ids)
        ladder_on = ladder_is_on(cfg, account)
        qual_norm = _account_qual_norm(states.values(), cfg)
        report.setdefault("qual_norm", {})[account] = float(qual_norm) if qual_norm is not None else None
        c_volume_min = min(n for n, _ in ladder_steps(cfg, account)) if ladder_on else c_min_mature
        price_by_norm = cfg.get("c_price_mode") == "account_norm"
        if price_by_norm:
            c_volume_min = min(c_volume_min, _int(cfg.get("c_price_min_mature"), 5))
        pending_c: list[tuple[dict, dict]] = []  # (base, summary) — C считается после эталона CPQ

        for ad in ads:
            live = lifetime.get(ad["ad_id"])
            spend = Decimal(str(live["spend"])).quantize(_CENT) if live is not None else None
            win = window.get(ad["ad_id"]) if daily else None
            spend_14d = Decimal(str(win["spend"])).quantize(_CENT) if win is not None else None
            state = states.get(ad["ad_id"])
            keep_forever = bool(state and state.get("keep_forever"))
            b_immune = keep_forever and not ladder_on  # лестница судит и объявления с квалами
            age_hours = float(ad["age_hours"])
            young = age_hours < max_age_days * 24
            excluded = is_excluded_from_rule_b((ad["name"], ad.get("adset_name", "")), cfg)

            wants_a = (
                young and modes["A"] != "off" and live is not None and prescreen_a is not None
                and spend is not None and spend >= prescreen_a
            )
            wants_b = (
                young and modes["B"] != "off" and not excluded and live is not None and age_hours >= b_hours
                and (live["fb_leads"] >= 1 or int((state or {}).get("amo_leads") or 0) >= 1)
            )
            wants_c = (
                daily and modes["C"] != "off" and not excluded and win is not None
                and (win["fb_leads"] >= c_volume_min or int((state or {}).get("amo_leads_14d") or 0) >= c_volume_min)
            )
            summary: dict | None = None
            if keep_forever and not wants_c and not (ladder_on and wants_b):
                summary = {
                    "leads": int(state.get("amo_leads") or 0),
                    "mature": int(state.get("amo_mature_leads") or 0),
                    "quals": max(1, int(state.get("amo_quals") or 0)),
                    "leads_14d": state.get("amo_leads_14d"), "mature_14d": state.get("amo_mature_14d"),
                    "quals_14d": state.get("amo_quals_14d"),
                }
                report["amo_cached"] += 1
            elif wants_a or wants_b or wants_c:
                fresh = _amo_is_fresh(state, moment) and (
                    not wants_c or _amo_is_fresh(state, moment, key="window_at", ttl=timedelta(hours=WINDOW_TTL_HOURS))
                )
                if fresh:
                    summary = {
                        "leads": int(state.get("amo_leads") or 0),
                        "mature": int(state.get("amo_mature_leads") or 0),
                        "quals": int(state.get("amo_quals") or 0),
                        "leads_14d": state.get("amo_leads_14d"), "mature_14d": state.get("amo_mature_14d"),
                        "quals_14d": state.get("amo_quals_14d"),
                    }
                    report["amo_cached"] += 1
                else:
                    summary = _fetch_amo_summary(ad["ad_id"], ad["created"], now=moment, b_hours=b_hours)
                    report["amo_queries"] += 1
                    if summary is not None:
                        try:
                            _save_ad_state(ad, account, summary, now=moment, previous=state)
                        except Exception as exc:  # noqa: BLE001 — кэш не роняет прогон
                            logger.warning("early_kill: состояние %s не сохранено — %s", ad["ad_id"], exc)

            amo_leads = summary["leads"] if summary else None
            mature = summary["mature"] if summary else None
            quals = summary["quals"] if summary else None

            base = {
                "run_id": run_id, "evaluated_at": moment.isoformat(),
                "account_id": account, "ad_id": ad["ad_id"], "ad_name": ad["name"],
                "adset_id": ad["adset_id"], "created_time": ad["created"].isoformat(),
                "age_hours": age_hours,
                "spend_usd": float(spend) if spend is not None else None,
                "impressions": live["impressions"] if live else None,
                "fb_leads": live["fb_leads"] if live else None,
                "amo_leads": amo_leads, "mature_leads": mature, "quals": quals,
                "cpl_ref_usd": float(cpl_ref) if cpl_ref is not None else None,
                "action": None, "proposal_id": None,
            }

            # Правило A (молодые)
            if young and modes["A"] != "off":
                if keep_forever or (quals is not None and quals >= 1):
                    verdict_a = EarlyKillVerdict("KEEP", "quals_present", None, None, 0)
                elif live is None:
                    verdict_a = EarlyKillVerdict("WAIT", "insufficient_lifetime_evidence", prescreen_a, no_lead_mult, 0)
                elif prescreen_a is None:
                    verdict_a = EarlyKillVerdict("WAIT", "no_cpl_reference", None, no_lead_mult, 0)
                elif not wants_a:
                    verdict_a = EarlyKillVerdict("WAIT", "spend_below_threshold", prescreen_a, no_lead_mult, 0)
                elif amo_leads is not None and amo_leads == 0 and live["fb_leads"] >= 1:
                    # FB видит лиды, AMO — нет: связка теряет заявки, а не реклама плохая.
                    logger.warning(
                        "early_kill: %s — FB-лидов %d, в AMO 0: связка теряет заявки, правило A ждёт",
                        ad["ad_id"], live["fb_leads"],
                    )
                    verdict_a = EarlyKillVerdict("WAIT", "fb_leads_without_amo", prescreen_a, no_lead_mult, 0)
                else:
                    verdict_a = evaluate_candidate(
                        account_id=account, spend=spend, amo_leads=amo_leads,
                        cpl_ref=cpl_ref, cfg_block=cfg, mode=modes["A"],
                    )
                evaluations.append({**base, "rule": "A", "mode": modes["A"], **_verdict_fields(verdict_a)})

            # Правило B (молодые, с 72 часов)
            if young and modes["B"] != "off" and age_hours >= b_hours:
                if excluded:
                    verdict_b = EarlyKillVerdict("WAIT", "excluded_by_marker", None, None, 0)
                elif b_immune:
                    verdict_b = EarlyKillVerdict("KEEP", "quals_present", None, None, 0)
                else:
                    verdict_b = evaluate_rule_b(
                        account_id=account, age_hours=age_hours, spend=spend,
                        amo_leads=amo_leads, mature_leads=mature, quals=quals,
                        cpl_ref=cpl_ref, cfg_block=cfg, mode=modes["B"], op_ok=op_ok, qual_norm=qual_norm,
                    )
                evaluations.append({**base, "rule": "B", "mode": modes["B"], **_verdict_fields(verdict_b)})

            # Правило S (суточный проход, все активные)
            if daily and modes["S"] != "off":
                verdict_s = evaluate_rule_s(
                    age_hours=age_hours, spend_14d=spend_14d,
                    fb_leads=live["fb_leads"] if live else None,
                    adset_active=adset_active_counts.get(ad["adset_id"], 0),
                    cfg_block=cfg, mode=modes["S"],
                )
                evaluations.append({
                    **base, "rule": "S", "mode": modes["S"],
                    "spend_usd": float(spend_14d) if spend_14d is not None else None,
                    **_verdict_fields(verdict_s),
                })

            # Правило C — откладываем до эталона CPQ кабинета
            if daily and modes["C"] != "off":
                if excluded:
                    evaluations.append({**base, "rule": "C", "mode": modes["C"],
                                        **_verdict_fields(EarlyKillVerdict("WAIT", "excluded_by_marker", None, None, 0))})
                elif not wants_c:
                    # Объёма для C нет (меньше c_min_mature_leads лидов по FB за окно) — AMO не спрашивали.
                    evaluations.append({**base, "rule": "C", "mode": modes["C"],
                                        "spend_usd": float(spend_14d) if spend_14d is not None else None,
                                        **_verdict_fields(EarlyKillVerdict("WAIT", "volume_below_min", None, None, 0))})
                else:
                    pending_c.append(({**base, "spend_usd": float(spend_14d) if spend_14d is not None else None,
                                       "amo_leads": (summary or {}).get("leads_14d"),
                                       "mature_leads": (summary or {}).get("mature_14d"),
                                       "quals": (summary or {}).get("quals_14d")}, {"spend_14d": spend_14d}))

        if pending_c:
            samples = [
                Decimal(str(b["spend_usd"])) / Decimal(int(b["quals"]))
                for b, _ in pending_c
                if b.get("quals") and int(b["quals"]) >= 3 and b.get("spend_usd")
            ]
            cpq_ref = median(samples).quantize(_CENT) if len(samples) >= 5 else cpq_fallback(cfg, account)
            if price_by_norm:
                # средняя цена квала кабинета за окно: весь расход активных / все их квалы
                pending_ids = {b["ad_id"] for b, _ in pending_c}
                acc_quals = sum(int(b.get("quals") or 0) for b, _ in pending_c) + sum(
                    int((st or {}).get("amo_quals_14d") or 0) for ad_id, st in states.items() if ad_id not in pending_ids
                )
                acc_spend = sum(Decimal(str(w["spend"])) for w in window.values() if w is not None)
                cpq_ref = (
                    (acc_spend / Decimal(acc_quals)).quantize(_CENT)
                    if acc_quals >= _int(cfg.get("c_cpq_norm_min_quals"), 10) and acc_spend > 0 else None
                )
            report["cpq_ref"][account] = float(cpq_ref) if cpq_ref else None
            for b, extra in pending_c:
                verdict_c = evaluate_rule_c(
                    spend_14d=extra["spend_14d"],
                    leads_14d=b["amo_leads"], mature_14d=b["mature_leads"], quals_14d=b["quals"],
                    cpq_ref=cpq_ref, cfg_block=cfg, mode=modes["C"], op_ok=op_ok,
                    account_id=account, qual_norm=qual_norm,
                )
                evaluations.append({**b, "rule": "C", "mode": modes["C"], "cpl_ref_usd": float(cpq_ref) if cpq_ref else None,
                                    **_verdict_fields(verdict_c)})

    would_pause = [r for r in evaluations if r["decision"] == "PAUSE"]
    for row in would_pause:
        row["action"] = "shadow"

    executed: list[dict] = []
    active_rows = [r for r in would_pause if modes[r["rule"]] == "active"]
    if active_rows:
        executed = _execute_rules(active_rows, cfg=cfg, now=moment, report=report, adset_active=adset_active_counts)

    already = _previously_flagged([(r["rule"], r["ad_id"]) for r in would_pause])
    shadow_fresh = [
        r for r in would_pause
        if r["action"] == "shadow" and (r["rule"], r["ad_id"]) not in already
    ]

    _record_evaluations(evaluations)
    report["recorded"] = len(evaluations)
    report["evaluated"] = len(evaluations)
    for rule, key in (("A", "would_pause_a"), ("B", "would_pause_b"), ("C", "would_pause_c"), ("S", "would_pause_s")):
        report[key] = [r["ad_id"] for r in would_pause if r["rule"] == rule]
    report["would_pause"] = sorted({r["ad_id"] for r in would_pause})
    report["executed"] = [r["ad_id"] for r in executed]
    report["ran"] = True

    _notify_executed(executed)
    if shadow_fresh:
        try:
            from services.notifications import send_telegram

            send_telegram(_shadow_message(shadow_fresh))
        except Exception as exc:  # noqa: BLE001 — сообщение не роняет прогон
            logger.warning("early_kill: сообщение «поймал бы» не ушло — %s", exc)
            report["errors"].append("telegram")
    return report


def _verdict_fields(verdict: EarlyKillVerdict) -> dict:
    return {
        "multiplier": float(verdict.multiplier) if verdict.multiplier is not None else None,
        "threshold_usd": float(verdict.threshold_usd) if verdict.threshold_usd is not None else None,
        "decision": verdict.decision,
        "reason": verdict.reason,
    }
