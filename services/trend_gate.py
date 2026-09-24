"""
Встраивание тренда недельных когорт в решения — волна 3.

Волна 1 (services/cohort_builder.py) сложила недели, волна 2
(services/trend_policy.py) научилась по ним судить: РОСТ / ПАДЕНИЕ / ПЛАТО /
МАЛО ДАННЫХ. Здесь — единственный мост между этим суждением и контурами,
которые двигают деньги (services/budget_scaler.py, services/decision_policy.py,
services/autopilot.py).

── Главный предохранитель ────────────────────────────────────────────────────
ТРЕНД НИКОГДА НЕ ЯВЛЯЕТСЯ ОСНОВАНИЕМ ПОДНЯТЬ БЮДЖЕТ. Ни РОСТ, ни любое другое
значение. Разрешены ровно два применения:

  (а) ВЕТО на подъём — адсет с action-grade ПАДЕНИЕМ не поднимается;
  (б) дополнительный вес к УЖЕ обоснованной паузе — тренд не порождает паузу
      сам по себе, он лишь добавляется к объявлению, которое и без него
      выглядит слабым.

Причина строгости: ошибка в данных о тренде не должна превращаться в
потраченные деньги. Поэтому вся асимметрия зашита в API — функции, которая
вернула бы «поднять», здесь просто нет.

── Что делает сбой чтения ────────────────────────────────────────────────────
НЕ fail-closed. Недоступная БД, отсутствующая таблица, битая строка когорты,
исключение внутри правила — всё это даёт ПУСТОЙ контекст: вето не применяется,
паузы не усиливаются, факт сбоя логируется и виден в TrendContext.error.
Здесь консервативнее именно НЕ вмешиваться: сбой аналитики не повод блокировать
подъём, который прошёл все денежные гейты скейлера (они остаются строгими и
fail-closed сами по себе).

── Режимы (autopilot.trend.mode) ─────────────────────────────────────────────
  off    — тренд не читается вообще, контекст пуст;
  shadow — ДЕФОЛТ. Тренд считается и попадает в отчёты («что бы он сделал»),
           но НЕ меняет ни одного исхода: ни одного лишнего вето, ни одной
           лишней паузы;
  active — тренд применяется по правилам (а) и (б).

Дефолт shadow означает, что после деплоя поведение бота не меняется. Перевод в
active — решение владельца через настройки, без деплоя (валидация значения —
web/settings_validation.py).

── Уровень ───────────────────────────────────────────────────────────────────
Только АДСЕТ. Уровень объявления в trend_policy никогда не даёт action_grade
(недельная дельта объявления — почти чистый шум), поэтому здесь он
не читается вовсе: не-action-grade вердикт не проходит ни в вето, ни в вес к
паузе.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Final

from services.trend_policy import (
    SustainProof,
    TrendLevel,
    TrendStatus,
    TrendVerdict,
    evaluate_cohort_rows,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SHADOW_SUFFIX",
    "TREND_DEFAULTS",
    "TrendContext",
    "TrendGateResult",
    "TrendMode",
    "describe_verdict",
    "empty_context",
    "evaluate_raise_veto",
    "load_trend_context",
    "resolve_mode",
]

# Часовой пояс рекламного кабинета: недели когорт нарезаны по его календарю
# (services/cohort_builder.py::_account_today), поэтому и возраст недели должен
# меряться им же — иначе неделя «дозревает» на сутки раньше, чем на самом деле.
try:  # pragma: no cover — ZoneInfo есть в рабочем окружении и в venv
    from zoneinfo import ZoneInfo

    _TZ_ACCOUNT = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover — фолбэк без tzdata
    _TZ_ACCOUNT = timezone(timedelta(hours=-8))

# Сколько последних недель тянуть из когорт: правилу нужны три закрытые
# дозревшие недели (W-3..W-1) плюс запас на текущую и на ещё не дозревшую.
_LOOKBACK_WEEKS: Final = 6

# Приписка к причине в shadow-режиме. Ровно одна формулировка на весь проект,
# чтобы отчёт скейлера и отчёт автопилота говорили одинаково.
SHADOW_SUFFIX: Final = " (тень: не применено)"

# Дефолт блока настроек. Единственный источник правды — services/autopilot.py
# домешивает его в AUTOPILOT_DEFAULTS.
TREND_DEFAULTS: Final = {"trend": {"mode": "shadow"}}


class TrendMode(str, Enum):
    """Режим встраивания тренда в решения."""

    OFF = "off"
    SHADOW = "shadow"
    ACTIVE = "active"


VALID_MODES: Final = tuple(mode.value for mode in TrendMode)


@dataclass(frozen=True)
class TrendGateResult:
    """Что тренд говорит про один адсет в одной точке применения.

    matched — тренд СКАЗАЛ БЫ «нельзя» (action-grade ПАДЕНИЕ есть).
    applied — это реально меняет исход (matched И режим active).
    reason  — человеческая формулировка для отчёта; в shadow несёт SHADOW_SUFFIX.

    Инвариант: applied → matched. Обратное неверно (shadow).
    """

    matched: bool = False
    applied: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class TrendContext:
    """Снимок тренда на прогон: режим + вердикты по адсетам + факт сбоя.

    Читается один раз за прогон и передаётся в чистые правила — так
    decision_policy остаётся без I/O, а скейлер не ходит в БД внутри цикла.
    """

    mode: TrendMode = TrendMode.SHADOW
    verdicts: Mapping[str, TrendVerdict] = field(default_factory=dict)
    error: str | None = None
    as_of: date | None = None

    @property
    def is_active(self) -> bool:
        """True только в режиме active — единственном, где тренд меняет исход."""
        return self.mode is TrendMode.ACTIVE

    @property
    def is_off(self) -> bool:
        return self.mode is TrendMode.OFF

    def verdict_for(self, adset_id: str | None) -> TrendVerdict | None:
        """Вердикт адсета, каким бы он ни был (включая ПЛАТО и МАЛО ДАННЫХ)."""
        if not adset_id:
            return None
        return self.verdicts.get(str(adset_id))

    def decline_for(self, adset_id: str | None) -> TrendVerdict | None:
        """Вердикт ТОЛЬКО если это подтверждённое падение с правом действовать.

        МАЛО ДАННЫХ, ПЛАТО, РОСТ и любой вердикт уровня объявления дают None —
        именно здесь живёт правило «тренд не поднимает и не паузит сам».
        """
        verdict = self.verdict_for(adset_id)
        if verdict is None:
            return None
        if not verdict.action_grade:
            return None
        if verdict.status is not TrendStatus.DECLINE:
            return None
        return verdict

    def describe(self, verdict: TrendVerdict) -> str:
        """Человеческая формулировка вердикта (см. describe_verdict)."""
        return describe_verdict(verdict)

    def mark_shadow(self, text: str) -> str:
        """Помечает формулировку как теневую — одной приставкой на весь проект.

        Метод, а не импорт константы: чистые правила (services/decision_policy.py)
        получают контекст объектом и остаются без зависимости на этот модуль.
        """
        return f"{text}{SHADOW_SUFFIX}"

    def counters(self) -> dict:
        """Сводка для отчётов: сколько адсетов и с каким статусом прочитано."""
        return {
            "mode": self.mode.value,
            "adsets": len(self.verdicts),
            "action_grade_declines": sum(
                1
                for verdict in self.verdicts.values()
                if verdict.action_grade and verdict.status is TrendStatus.DECLINE
            ),
            "error": self.error,
        }


def empty_context(
    mode: TrendMode = TrendMode.SHADOW,
    *,
    error: str | None = None,
    as_of: date | None = None,
) -> TrendContext:
    """Контекст без вердиктов: тренд ничего не запрещает и ничего не усиливает."""
    return TrendContext(mode=mode, verdicts={}, error=error, as_of=as_of)


def resolve_mode(cfg: Mapping | None) -> TrendMode:
    """Режим из конфига автопилота. Любая неоднозначность → дефолт shadow.

    Файл settings.json можно поправить в обход HTTP-валидатора, поэтому
    неизвестное значение НЕ становится разрешающим режимом: оно деградирует до
    shadow, где тренд ничего не меняет.
    """
    try:
        block = (cfg or {}).get("trend")
        raw = (block or {}).get("mode") if isinstance(block, Mapping) else None
    except Exception:  # pragma: no cover — конфиг не Mapping
        raw = None
    if isinstance(raw, str):
        try:
            return TrendMode(raw.strip().lower())
        except ValueError:
            logger.warning(
                "trend_gate: неизвестный autopilot.trend.mode=%r — считаем shadow", raw
            )
    return TrendMode.SHADOW


def describe_verdict(verdict: TrendVerdict) -> str:
    """Вердикт → фраза человеческим языком для отчёта.

    Примеры: «тренд: квал 28% → 16% три недели подряд»,
             «тренд: квал 31% → 19% неделя к неделе».
    Презентация живёт здесь, а не в правиле: trend_policy не знает про отчёты.
    """
    recent = verdict.recent
    previous = verdict.previous
    earlier = verdict.earlier
    three_weeks = (
        verdict.sustained_by is SustainProof.THREE_WEEKS
        and earlier is not None
        and earlier.qual_pct is not None
    )
    start = earlier if three_weeks else previous
    if recent is None or recent.qual_pct is None or start is None or start.qual_pct is None:
        # Числа без полных недель не выдаём — та же дисциплина, что в правиле.
        return f"тренд: {verdict.status.value} ({verdict.reason})"
    proof = "три недели подряд" if three_weeks else "неделя к неделе"
    return (
        f"тренд: квал {start.qual_pct:.0f}% → {recent.qual_pct:.0f}% {proof}"
    )


def evaluate_raise_veto(ctx: TrendContext | None, adset_id: str | None) -> TrendGateResult:
    """Правило (а): можно ли поднимать бюджет этого адсета.

    Возвращает matched=True, если у адсета action-grade ПАДЕНИЕ. applied=True
    только в режиме active — в shadow исход не меняется, но причина уже
    сформулирована и попадёт в отчёт с пометкой «тень».

    Пустой контекст (режим off, сбой чтения, нет данных по адсету) даёт
    matched=False: подъём не блокируется из-за сбоя аналитики.
    """
    if ctx is None:
        return TrendGateResult()
    verdict = ctx.decline_for(adset_id)
    if verdict is None:
        return TrendGateResult()
    reason = describe_verdict(verdict)
    if ctx.is_active:
        return TrendGateResult(matched=True, applied=True, reason=reason)
    return TrendGateResult(matched=True, applied=False, reason=reason + SHADOW_SUFFIX)


def load_trend_context(
    *,
    cfg: Mapping | None = None,
    as_of: date | None = None,
    weeks: int = _LOOKBACK_WEEKS,
) -> TrendContext:
    """Читает когорты и считает вердикты по адсетам. Никогда не бросает.

    Любой сбой (нет БД, нет таблицы, битая строка, исключение правила) даёт
    пустой контекст с заполненным error — см. «Что делает сбой чтения» в шапке.
    """
    if cfg is None:
        cfg = _autopilot_cfg()
    mode = resolve_mode(cfg)
    if mode is TrendMode.OFF:
        logger.debug("trend_gate: режим off — когорты не читаем")
        return empty_context(mode)

    as_of = as_of or datetime.now(_TZ_ACCOUNT).date()
    try:
        rows = _fetch_cohort_rows(as_of=as_of, weeks=weeks)
    except Exception as exc:  # pragma: no cover — _fetch_cohort_rows сам ловит
        logger.error("trend_gate: чтение когорт упало — %s", exc)
        return empty_context(mode, error=f"cohort_read_error: {exc}", as_of=as_of)
    if rows is None:
        return empty_context(mode, error="cohort_read_error", as_of=as_of)
    if not rows:
        logger.info("trend_gate: строк когорт нет — тренд в этом прогоне не участвует")
        return empty_context(mode, as_of=as_of)

    try:
        verdicts = evaluate_cohort_rows(rows, level=TrendLevel.ADSET, as_of=as_of)
    except Exception as exc:
        logger.error("trend_gate: правило тренда упало — %s", exc)
        return empty_context(mode, error=f"trend_rule_error: {exc}", as_of=as_of)

    by_adset = {verdict.entity_id: verdict for verdict in verdicts}
    ctx = TrendContext(mode=mode, verdicts=by_adset, error=None, as_of=as_of)
    counters = ctx.counters()
    logger.info(
        "trend_gate: режим %s, строк когорт %d, адсетов %d, "
        "падений с правом действовать %d",
        counters["mode"], len(rows), counters["adsets"],
        counters["action_grade_declines"],
    )
    return ctx


# ---------------------------------------------------------------------------
# Внутренняя механика
# ---------------------------------------------------------------------------

def _autopilot_cfg() -> Mapping:
    """Конфиг автопилота. Недоступен → пустой словарь (режим станет shadow)."""
    try:
        from services.autopilot import get_autopilot_config

        return get_autopilot_config() or {}
    except Exception as exc:
        logger.warning("trend_gate: конфиг автопилота недоступен — %s", exc)
        return {}


def _fetch_cohort_rows(*, as_of: date, weeks: int) -> list[dict] | None:
    """Строки ad_weekly_cohorts за последние `weeks` недель. Один SQL по индексу.

    None — прочитать не удалось (это отмечается в error контекста). Пустой
    список — читать нечего: БД не инициализирована или таблицы ещё нет
    (миграция 024 не применена), и это НЕ сбой.

    Отдельный ридер, а не вызов services/shadow_report.py: тот модуль — витрина
    теневого отчёта, боевой контур не должен зависеть от её приватных функций.
    """
    from services.creative_intelligence import DB_PATH

    if DB_PATH is None:
        logger.debug("trend_gate: DB_PATH не инициализирован — тренд не считаем")
        return []

    try:
        conn = sqlite3.connect(DB_PATH)
    except Exception as exc:
        logger.error("trend_gate: не удалось открыть БД — %s", exc)
        return None
    conn.row_factory = sqlite3.Row
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            ("ad_weekly_cohorts",),
        ).fetchone()
        if table is None:
            logger.info(
                "trend_gate: таблицы ad_weekly_cohorts нет (миграция 024 не "
                "применена) — тренд не считаем"
            )
            return []
        cutoff = (as_of - timedelta(days=7 * weeks)).isoformat()
        rows = conn.execute(
            """
            SELECT ad_id, ad_name, adset_id, adset_name, week_start,
                   spend_usd, amo_leads, quals, comparable, not_comparable_reason
            FROM ad_weekly_cohorts
            WHERE week_start >= ?
            ORDER BY week_start, ad_id
            """,
            (cutoff,),
        ).fetchall()
        return [dict(row) for row in rows]
    except Exception as exc:
        logger.error("trend_gate: ошибка чтения когорт — %s", exc)
        return None
    finally:
        conn.close()
