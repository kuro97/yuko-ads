"""
Правило тренда недельных когорт — волна 2: суждение о траектории.

Зачем: бот судит рекламу по среднему за месяц и не видит динамику. «ROMI 500%
за месяц» одинаково выглядит у выдыхающейся звезды (первая неделя 1500%,
последняя 50%) и у разгоняющейся ракеты (было 50%, стало 200%). Волна 1
(services/cohort_builder.py, миграция 024) сложила данные по парам
(объявление, неделя). Здесь — правило, которое читает эти недели и отвечает:
РОСТ, ПАДЕНИЕ, ПЛАТО или МАЛО ДАННЫХ.

Границы волны 2. Только суждение и теневой вывод: в решения тренд НЕ
встраивается, бюджеты не двигает, ничего не пишет. Модуль чистый — ни сети,
ни БД, ни settings, ни часов: «сегодня» приходит параметром as_of, недели —
списком точек.

Метрика первого этапа — квал-процент (quals / amo_leads). Расход и CPL
считаются вторым, СПРАВОЧНЫМ сигналом: они видны в вердикте, но ни на статус,
ни на action_grade не влияют — денежных порогов здесь нет.

── ROMI: считается, но СПРАВОЧНО ─────────────────────────────────────────────
Волна 4 (миграция 025, services/cohort_builder.py) дала неделе когортную
выручку: деньги от лидов, СОЗДАННЫХ на этой неделе, за фиксированный горизонт
после каждого лида. Поэтому ROMI недели здесь считается и показывается —
рядом с квалом, из сумм, а не усреднением процентов.

Но статуса он не меняет и action_grade не даёт НИКОГДА, и вот почему. Пороги
шума в этом модуле не выдуманы, а получаются замером на исторических данных
(в коде лежит пример значений; перемерьте на своих данных) — и замеряются для
КВАЛ-ПРОЦЕНТА. Для ROMI такого замера нет. Перенести на него таблицу
NOISE_FLOOR_PP нельзя даже приблизительно: квал-процент это доля с
биномиальной ошибкой ~1/sqrt(n), а ROMI — отношение сумм с тяжёлым правым
хвостом (одна крупная рассрочка сдвигает неделю сильнее, чем десяток лидов),
и его недельная волатильность заведомо ВЫШЕ при том же объёме. Порог,
взятый из головы, дал бы «ПАДЕНИЕ ROMI» на шуме — и это худший исход, чем
отсутствие сигнала: бот выключал бы работающие объявления.

Как этот порог получить, когда накопятся когорты (метод тот же, каким
получена таблица квала): взять все пары СОСЕДНИХ дозревших недель одной
сущности с revenue_mature=1 и одинаковым revenue_horizon_days, посчитать
|дельту ROMI| по бакетам минимального объёма пары (лиды и/или оплаты) и взять
90-й процентиль как порог бакета — плюс отдельно проверить устойчивость к
выбросу, выбросив максимальный платёж недели. До этого замера ROMI остаётся
наблюдением: romi_recent/romi_previous/romi_change_pp в вердикте есть, влияния
на TrendStatus и action_grade — нет.

── Почему пороги именно такие ────────────────────────────────────────────────
Ниже — пример значений; перемерьте на своих данных. Метод замера: расход по
объявлениям × день и лиды CRM за несколько недель; для каждой пары СОСЕДНИХ
недель считается |дельта квал-процента|, порог бакета — её 90-й процентиль по
бакетам минимального объёма пары (эти значения и лежат в _NOISE_P90_MEASURED).

Уровень ОБЪЯВЛЕНИЯ:
    лидов   90-й процентиль
    1–4      50 п.п.
    5–9      35 п.п.
    10–19    23 п.п.
    20–39    16 п.п.
    40–79     7,6 п.п.

Уровень АДСЕТА (у большинства адсет-недель объём 40+ лидов):
    лидов   90-й процентиль
    1–19     60 п.п.
    20–39     8,7 п.п.
    40–79    16,6 п.п.
    80–159   13,8 п.п.
    160+      7,1 п.п.

Три вывода, которые и определили конструкцию:
  1. На уровне ОБЪЯВЛЕНИЯ недельная дельта — почти чистый шум до ~40 лидов в
     неделю, а таких объявлений единицы. Поэтому боевой уровень — АДСЕТ, а
     объявление считается только как диагностика: action_grade у него НИКОГДА
     не выставляется (см. _resolve_action_grade).
  2. Порог «5 п.п.» не работает нигде: даже на адсетах при 80–159 лидах
     90-й процентиль шума 13,8 п.п. Поэтому минимальная дельта берётся из
     таблицы по объёму, а не константой.
  3. Шум падает медленнее биномиальной теории — значит в нём есть реальная
     недельная волатильность, а не только ошибка выборки. Поэтому одиночная
     пара недель не даёт права действовать: нужна либо согласованность
     направления на трёх последних закрытых неделях, либо дельта с запасом
     (SUSTAIN_MARGIN) — см. _find_proof.

Зрелость. Текущая неполная неделя занижает квал примерно вдвое против прошлой;
более старые недели стабильны. Лаг квалификации короткий: большая часть квалов
проставляется за первые дни, почти все — за неделю. Отсюда
MIN_WEEK_AGE_DAYS = 7: неделя участвует в сравнении только когда с её
воскресенья прошла неделя. Текущая неделя не участвует
никогда — её возраст отрицателен.

Fail-closed. Неполные данные (comparable=0 в когортах) НИКОГДА не дают числа:
результат — МАЛО ДАННЫХ с причиной. Та же дисциплина, что у
NULL_COERCED_TO_ZERO в services/approval_rules.py.

Переиспользование. EvidenceStatus взят из services/kill_policy.py — это ровно
тот же словарь полноты доказательств. Модель зрелости kill_policy
(mature_leads / maturity_profile) НЕ переиспользуется: там зрелость — свойство
ЛИДА (дожил ли он до события квалификации), здесь — свойство НЕДЕЛИ
(календарный возраст когорты). Decision (KEEP/PAUSE/WAIT) тоже не берётся
намеренно: тренд волны 2 описывает наблюдение, а не действие.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from datetime import timedelta
from enum import Enum
from typing import Final

from services.kill_policy import EvidenceStatus

__all__ = [
    "MIN_WEEK_AGE_DAYS",
    "MIN_WEEK_LEADS",
    "NOISE_FLOOR_PP",
    "SUSTAIN_MARGIN",
    "SustainProof",
    "TrendLevel",
    "TrendPolicyError",
    "TrendStatus",
    "TrendVerdict",
    "TrendWeek",
    "WeekPoint",
    "aggregate_week_points",
    "evaluate_cohort_rows",
    "evaluate_trend",
    "noise_floor_pp",
    "parse_week_point",
    "week_age_days",
    "wilson_interval",
]


class TrendPolicyError(ValueError):
    """Вход правила тренда небезопасен или неоднозначен."""


class TrendLevel(str, Enum):
    """Уровень, на котором считается тренд.

    ADSET — боевой: только он может дать action-grade вердикт.
    AD — диагностика: объёмов объявления не хватает на суждение (см. замер).
    """

    ADSET = "adset"
    AD = "ad"


class TrendStatus(str, Enum):
    """Статус траектории.

    РОСТ/ПАДЕНИЕ выставляются ТОЛЬКО когда пройдены все проверки: полнота,
    объём, порог шума, непересечение интервалов Уилсона и устойчивость.
    Всё остальное — ПЛАТО (подтверждённого тренда нет) или МАЛО ДАННЫХ
    (сравнивать нечего). Наблюдаемое направление при этом не теряется: оно
    видно в TrendVerdict.delta_pp и .observed_direction.
    """

    GROWTH = "РОСТ"
    DECLINE = "ПАДЕНИЕ"
    PLATEAU = "ПЛАТО"
    INSUFFICIENT = "МАЛО ДАННЫХ"


class SustainProof(str, Enum):
    """Чем подтверждена устойчивость движения."""

    THREE_WEEKS = "three_weeks"
    MARGIN = "margin"


# ---------------------------------------------------------------------------
# Константы правила
# ---------------------------------------------------------------------------

# Ниже этого объёма недельная дельта не оценивается вообще — «без исключений».
# 20 лидов — нижняя граница первого бакета, где 90-й процентиль шума на адсете
# перестаёт быть трёхзначно бессмысленным (1–19 лидов → 60 п.п.).
MIN_WEEK_LEADS: Final = 20

# Возраст недели = сколько дней прошло с её воскресенья. Неделя участвует в
# сравнении только с 7 дней: почти все квалы проставляются за 7 дней после
# лида, а незрелая неделя занижает квал примерно вдвое (см. докстринг модуля).
MIN_WEEK_AGE_DAYS: Final = 7

# Во сколько раз одиночная пара недель должна перекрыть порог шума, чтобы её
# приняли без подтверждения третьей неделей. Двукратный запас к 90-му
# процентилю шума: такое движение волатильностью не объясняется.
SUSTAIN_MARGIN: Final = 2.0

# z для двустороннего 95%-интервала. scipy сознательно не используется: он не
# заявлен в requirements.txt (в venv попал транзитивно), а для интервала
# Уилсона нужна одна константа нормального квантиля.
Z_95: Final = 1.959963984540054

# 90-й процентиль |недельной дельты квал-процента| по бакетам минимального
# объёма пары. Пример значений; перемерьте на своих данных тем же методом
# (см. докстринг модуля). Ключ бакета — нижняя граница.
_NOISE_P90_MEASURED: Final[dict[TrendLevel, tuple[tuple[int, float], ...]]] = {
    TrendLevel.ADSET: (
        (1, 60.0),
        (20, 8.7),
        (40, 16.6),
        (80, 13.8),
        (160, 7.1),
    ),
    TrendLevel.AD: (
        (1, 50.0),
        (5, 35.0),
        (10, 23.0),
        (20, 16.0),
        (40, 7.6),
    ),
}


def _monotone_envelope(
    table: tuple[tuple[int, float], ...],
) -> tuple[tuple[int, float], ...]:
    """Делает порог невозрастающим по объёму: меньше данных — не мягче порог.

    Пример замера на адсетах немонотонен: бакет 20–39 показал 8,7 п.п., а
    40–79 — 16,6 п.п. (в редком бакете просто мало пар недель). Брать таблицу как есть
    нельзя: адсет на 30 лидов проходил бы по 8,7 п.п. то, что на 50 лидах
    считается шумом. Поэтому порог бакета поднимается до максимума среди всех
    бо́льших объёмов.
    """
    envelope: list[tuple[int, float]] = []
    running = 0.0
    for lower, value in reversed(table):
        running = max(running, value)
        envelope.append((lower, running))
    return tuple(reversed(envelope))


def _at_least_adset(
    ad_table: tuple[tuple[int, float], ...],
    adset_table: tuple[tuple[int, float], ...],
) -> tuple[tuple[int, float], ...]:
    """Порог на объявлении не может быть мягче, чем на адсете при том же объёме.

    Замер на объявлениях обрывается на бакете 40–79 (7,6 п.п.): объявлений с
    бо́льшим недельным объёмом единицы, мерить не на чем. Продлевать 7,6 п.п.
    вверх нельзя — на адсетах при 80–159 лидах шум измерен как 13,8 п.п., а
    объявление уже адсета и тише него не бывает. Поэтому берётся максимум из
    двух таблиц по объединённым границам бакетов.
    """
    bounds = sorted({lower for lower, _ in (*ad_table, *adset_table)})

    def value_at(table: tuple[tuple[int, float], ...], leads: int) -> float:
        found = table[0][1]
        for lower, value in table:
            if leads >= lower:
                found = value
        return found

    return tuple(
        (bound, max(value_at(ad_table, bound), value_at(adset_table, bound)))
        for bound in bounds
    )


_ADSET_FLOOR: Final = _monotone_envelope(_NOISE_P90_MEASURED[TrendLevel.ADSET])

# Рабочая таблица порогов: измеренная, поднятая до монотонной огибающей, а на
# уровне объявления — ещё и до уровня адсета (см. _at_least_adset).
NOISE_FLOOR_PP: Final[dict[TrendLevel, tuple[tuple[int, float], ...]]] = {
    TrendLevel.ADSET: _ADSET_FLOOR,
    TrendLevel.AD: _at_least_adset(
        _monotone_envelope(_NOISE_P90_MEASURED[TrendLevel.AD]),
        _ADSET_FLOOR,
    ),
}


# Коды причин. Формат «КОД» или «КОД:деталь» — как в cohort_builder.
REASON_NO_MATURE_WEEK: Final = "NO_MATURE_WEEK"
REASON_PREV_WEEK_MISSING: Final = "PREV_WEEK_MISSING"
REASON_WEEK_NOT_COMPARABLE: Final = "WEEK_NOT_COMPARABLE"
REASON_VOLUME_BELOW_MIN: Final = "VOLUME_BELOW_MIN"
REASON_DELTA_BELOW_NOISE: Final = "DELTA_BELOW_NOISE"
REASON_WILSON_OVERLAP: Final = "WILSON_OVERLAP"
REASON_NOT_SUSTAINED: Final = "NOT_SUSTAINED"
REASON_TREND_CONFIRMED: Final = "TREND_CONFIRMED"
REASON_AD_LEVEL_DIAGNOSTIC: Final = "AD_LEVEL_DIAGNOSTIC_ONLY"


# ---------------------------------------------------------------------------
# Модель входа
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WeekPoint:
    """Одна неделя одной сущности — вход правила.

    Повторяет дисциплину строки ad_weekly_cohorts: comparable=1 обязана быть
    полной и без причины, comparable=0 обязана причину назвать.

    Денежная часть (волна 4) приходит КОМПОНЕНТАМИ, а не готовым ROMI: на
    уровне адсета проценты объявлений складывать нельзя, ROMI считается из
    сумм выручки и расхода. revenue_lcy допускает отрицательные значения —
    возврат обязан уменьшать выручку, а не обнуляться.
    """

    week_start: date
    amo_leads: int | None
    quals: int | None
    comparable: bool
    not_comparable_reason: str | None = None
    spend_usd: float | None = None
    revenue_lcy: float | None = None
    usd_lcy_rate: float | None = None
    revenue_mature: bool = False
    revenue_horizon_days: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.week_start, date):
            raise TrendPolicyError("week.week_start должен быть date")
        if self.week_start.weekday() != 0:
            raise TrendPolicyError(
                f"week.week_start должен быть понедельником, получено "
                f"{self.week_start.isoformat()}"
            )
        if not isinstance(self.comparable, bool):
            raise TrendPolicyError("week.comparable должен быть bool")
        _validate_optional_nonnegative_int(self.amo_leads, "week.amo_leads")
        _validate_optional_nonnegative_int(self.quals, "week.quals")
        _validate_optional_nonnegative_float(self.spend_usd, "week.spend_usd")
        _validate_optional_signed_float(self.revenue_lcy, "week.revenue_lcy")
        _validate_optional_positive_float(self.usd_lcy_rate, "week.usd_lcy_rate")
        if not isinstance(self.revenue_mature, bool):
            raise TrendPolicyError("week.revenue_mature должен быть bool")
        if self.revenue_horizon_days is not None and (
            isinstance(self.revenue_horizon_days, bool)
            or not isinstance(self.revenue_horizon_days, int)
            or self.revenue_horizon_days < 1
        ):
            raise TrendPolicyError("week.revenue_horizon_days должен быть int >= 1")
        if (
            self.amo_leads is not None
            and self.quals is not None
            and self.quals > self.amo_leads
        ):
            raise TrendPolicyError("week.quals не может превышать amo_leads")
        if self.comparable:
            if self.amo_leads is None or self.quals is None:
                raise TrendPolicyError(
                    "week.comparable=True требует известных amo_leads и quals"
                )
            if self.not_comparable_reason is not None:
                raise TrendPolicyError(
                    "week.comparable=True не может нести причину несравнимости"
                )
        elif not (
            isinstance(self.not_comparable_reason, str)
            and self.not_comparable_reason.strip()
        ):
            raise TrendPolicyError(
                "week.comparable=False обязана назвать not_comparable_reason"
            )

    @property
    def qual_pct(self) -> float | None:
        """Квал-процент недели. None, если считать не из чего."""
        if not self.amo_leads or self.quals is None:
            return None
        return self.quals / self.amo_leads * 100.0

    @property
    def cpl_usd(self) -> float | None:
        """Справочный CPL: расход недели на лид AMO (не на лид FB)."""
        if self.spend_usd is None or not self.amo_leads:
            return None
        return self.spend_usd / self.amo_leads

    @property
    def romi_pct(self) -> float | None:
        """ROMI недели, % (100% — вышли в ноль). None, если считать не из чего.

        Требуется ВСЁ сразу: горизонт выручки закрыт (revenue_mature),
        выручка известна, расход известен и не ноль, курс той недели известен.
        Ни одной подстановки: недозревшая когорта, недоступная ERP и
        отсутствующий курс дают None, а не оптимистичное число.
        """
        if not self.revenue_mature:
            return None
        if self.revenue_lcy is None or self.usd_lcy_rate is None:
            return None
        if not self.spend_usd:
            return None
        return self.revenue_lcy / (self.spend_usd * self.usd_lcy_rate) * 100.0


@dataclass(frozen=True)
class TrendWeek:
    """Неделя в том виде, в каком она попала в вердикт (готово для отчёта)."""

    week_start: date
    week_end: date
    age_days: int
    leads: int | None
    quals: int | None
    qual_pct: float | None
    spend_usd: float | None
    cpl_usd: float | None
    comparable: bool
    not_comparable_reason: str | None
    # Денежная часть (волна 4). revenue_mature — календарное свойство недели:
    # дозрел ли горизонт сбора выручки. revenue_days_elapsed показывает, сколько
    # дней горизонта уже прошло, чтобы отчёт мог честно сказать «зреет 6 из 14».
    revenue_lcy: float | None = None
    romi_pct: float | None = None
    revenue_mature: bool = False
    revenue_horizon_days: int | None = None
    revenue_days_elapsed: int | None = None


@dataclass(frozen=True)
class TrendVerdict:
    """Результат правила по одной сущности.

    status — что видно; action_grade — есть ли право на этом действовать.
    Право даёт ТОЛЬКО уровень адсета и только при подтверждённом тренде;
    объявление остаётся диагностикой при любых цифрах.
    """

    level: TrendLevel
    entity_id: str
    entity_name: str | None
    status: TrendStatus
    action_grade: bool
    evidence: EvidenceStatus
    reason: str
    recent: TrendWeek | None = None
    previous: TrendWeek | None = None
    earlier: TrendWeek | None = None
    delta_pp: float | None = None
    total_delta_pp: float | None = None
    noise_floor_pp: float | None = None
    min_leads: int | None = None
    wilson_recent: tuple[float, float] | None = None
    wilson_previous: tuple[float, float] | None = None
    wilson_separated: bool | None = None
    sustained_by: SustainProof | None = None
    cpl_change_pct: float | None = None
    # ROMI — второй, СПРАВОЧНЫЙ сигнал (см. докстринг модуля). Ни на status, ни
    # на action_grade не влияет: порог шума для ROMI не замерен, а выдуманный
    # порог выключал бы работающие объявления на волатильности.
    romi_recent: float | None = None
    romi_previous: float | None = None
    romi_change_pp: float | None = None

    @property
    def observed_direction(self) -> str | None:
        """Наблюдаемое направление независимо от того, подтверждено ли оно."""
        if self.delta_pp is None:
            return None
        if self.delta_pp > 0:
            return "вверх"
        if self.delta_pp < 0:
            return "вниз"
        return "ровно"


# ---------------------------------------------------------------------------
# Календарь и статистика
# ---------------------------------------------------------------------------

def week_end_of(week_start: date) -> date:
    """Воскресенье недели, начинающейся в week_start."""
    return week_start + timedelta(days=6)


def week_age_days(week_start: date, as_of: date) -> int:
    """Возраст недели в днях: сколько прошло с её воскресенья.

    Текущая (незакрытая) неделя даёт отрицательный возраст и потому никогда
    не проходит порог MIN_WEEK_AGE_DAYS.
    """
    return (as_of - week_end_of(week_start)).days


def wilson_interval(
    successes: int,
    total: int,
    *,
    z: float = Z_95,
) -> tuple[float, float]:
    """Доверительный интервал Уилсона для доли (доли, не процента).

    Реализован здесь намеренно: scipy не заявлен в requirements.txt, а всё,
    что нужно интервалу, — квантиль нормального распределения константой.

    Формула:
        center = (p + z²/2n) / (1 + z²/n)
        half   = z/(1 + z²/n) · sqrt(p(1−p)/n + z²/4n²)

    Проверочные значения: (50 из 100) → (0.4038, 0.5962); (0 из 10) →
    (0.0, 0.2775) — в отличие от нормальной аппроксимации, у нуля успехов
    интервал не схлопывается в точку.
    """
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        raise TrendPolicyError("wilson_interval: total должен быть int > 0")
    if isinstance(successes, bool) or not isinstance(successes, int):
        raise TrendPolicyError("wilson_interval: successes должен быть int")
    if not 0 <= successes <= total:
        raise TrendPolicyError(
            "wilson_interval: successes должен лежать в [0, total]"
        )
    if not isinstance(z, (int, float)) or isinstance(z, bool) or z <= 0:
        raise TrendPolicyError("wilson_interval: z должен быть числом > 0")

    p = successes / total
    z_sq = z * z
    denominator = 1.0 + z_sq / total
    center = (p + z_sq / (2 * total)) / denominator
    half = (
        z
        / denominator
        * math.sqrt(p * (1 - p) / total + z_sq / (4 * total * total))
    )
    return (max(0.0, center - half), min(1.0, center + half))


def noise_floor_pp(level: TrendLevel, leads: int) -> float:
    """Минимальная значимая дельта (п.п.) для объёма leads на уровне level."""
    if not isinstance(level, TrendLevel):
        raise TrendPolicyError("noise_floor_pp: level должен быть TrendLevel")
    if isinstance(leads, bool) or not isinstance(leads, int) or leads < 0:
        raise TrendPolicyError("noise_floor_pp: leads должен быть int >= 0")
    table = NOISE_FLOOR_PP[level]
    floor = table[0][1]
    for lower, value in table:
        if leads >= lower:
            floor = value
    return floor


# ---------------------------------------------------------------------------
# Разбор и агрегация
# ---------------------------------------------------------------------------

def parse_week_point(raw: Mapping[str, object]) -> WeekPoint:
    """Строгий разбор строки ad_weekly_cohorts в точку недели.

    Лишние колонки игнорируются намеренно — в отличие от kill_policy, где
    неизвестный ключ считается ошибкой конфигурации: здесь вход не конфиг, а
    широкая строка таблицы, и правилу нужна её часть.
    """
    if not isinstance(raw, Mapping):
        raise TrendPolicyError("строка когорты должна быть Mapping")
    return WeekPoint(
        week_start=_require_date(raw.get("week_start"), "week_start"),
        amo_leads=_optional_int(raw.get("amo_leads"), "amo_leads"),
        quals=_optional_int(raw.get("quals"), "quals"),
        comparable=_require_flag(raw.get("comparable"), "comparable"),
        not_comparable_reason=_optional_text(
            raw.get("not_comparable_reason"), "not_comparable_reason"
        ),
        spend_usd=_optional_float(raw.get("spend_usd"), "spend_usd"),
        # Денежные колонки появились миграцией 025: строка из БД без них
        # (старая фикстура, чтение до применения миграции) не ошибка — просто
        # выручка неизвестна.
        revenue_lcy=_optional_signed_float(raw.get("revenue_lcy"), "revenue_lcy"),
        usd_lcy_rate=_optional_positive_float(
            raw.get("usd_lcy_rate"), "usd_lcy_rate"
        ),
        revenue_mature=_optional_flag(
            raw.get("revenue_mature"), "revenue_mature"
        ),
        revenue_horizon_days=_optional_int(
            raw.get("revenue_horizon_days"), "revenue_horizon_days"
        ),
    )


def aggregate_week_points(points: Iterable[WeekPoint]) -> WeekPoint:
    """Складывает точки ОДНОЙ недели в одну (например, объявления адсета).

    NULL заразителен: если хотя бы у одной части метрика неизвестна, сумма
    неизвестна — иначе адсет получил бы занижённый расход и заниженный объём.
    Сравнимость — конъюнкция: одна дырявая строка делает несравнимой всю
    неделю адсета, причины склеиваются через ';'.
    """
    items = list(points)
    if not items:
        raise TrendPolicyError("aggregate_week_points: пустой список точек")
    week_start = items[0].week_start
    if any(item.week_start != week_start for item in items):
        raise TrendPolicyError(
            "aggregate_week_points: точки принадлежат разным неделям"
        )

    leads = _sum_or_none(item.amo_leads for item in items)
    quals = _sum_or_none(item.quals for item in items)
    spend_total = _sum_or_none(item.spend_usd for item in items)
    spend = None if spend_total is None else float(spend_total)
    comparable = all(item.comparable for item in items)

    # Деньги складываются по тем же правилам: один неизвестный слагаемый
    # обнуляет знание о сумме. Зрелость — конъюнкция: пока хоть одна часть
    # адсета зреет, ROMI адсета считать нельзя. Горизонт и курс должны у всех
    # совпадать — иначе сумма собрана из разных метрик, и она неизвестна.
    revenue_total = _sum_or_none(item.revenue_lcy for item in items)
    revenue = None if revenue_total is None else float(revenue_total)
    revenue_mature = all(item.revenue_mature for item in items)
    rate = _single_value_or_none(item.usd_lcy_rate for item in items)
    horizon = _single_value_or_none(item.revenue_horizon_days for item in items)

    reason: str | None = None
    if not comparable:
        codes: list[str] = []
        for item in items:
            if item.comparable or not item.not_comparable_reason:
                continue
            for chunk in item.not_comparable_reason.split(";"):
                chunk = chunk.strip()
                if chunk and chunk not in codes:
                    codes.append(chunk)
        reason = ";".join(codes) or REASON_WEEK_NOT_COMPARABLE

    return WeekPoint(
        week_start=week_start,
        amo_leads=leads,
        quals=quals,
        comparable=comparable,
        not_comparable_reason=reason,
        spend_usd=spend,
        revenue_lcy=revenue,
        usd_lcy_rate=rate,
        revenue_mature=revenue_mature,
        revenue_horizon_days=horizon,
    )


# ---------------------------------------------------------------------------
# Правило
# ---------------------------------------------------------------------------

def evaluate_trend(
    points: Iterable[WeekPoint],
    *,
    level: TrendLevel,
    entity_id: str,
    as_of: date,
    entity_name: str | None = None,
) -> TrendVerdict:
    """Считает тренд по неделям одной сущности. Без I/O и без мутаций.

    Порядок проверок (первая сработавшая и объясняет вердикт):
      1. Есть ли дозревшая закрытая неделя W-1 (возраст ≥ MIN_WEEK_AGE_DAYS).
      2. Есть ли календарно соседняя W-2 (ровно неделей раньше).
      3. Обе ли сравнимы (comparable=1 в когортах). Нет → МАЛО ДАННЫХ.
      4. Хватает ли объёма (≥ MIN_WEEK_LEADS в каждой). Нет → МАЛО ДАННЫХ.
      5. Есть ли доказательство устойчивого движения (_find_proof): три
         недели в одну сторону с суммарной дельтой выше порога шума и
         непересечением интервалов Уилсона на краях — либо одиночная пара с
         дельтой в SUSTAIN_MARGIN порогов и непересечением интервалов пары.
    Доказательство найдено → РОСТ/ПАДЕНИЕ; нет → ПЛАТО с причиной; шаги 1–4
    не пройдены → МАЛО ДАННЫХ. Числа при неполных данных не выдаются никогда.
    """
    if not isinstance(level, TrendLevel):
        raise TrendPolicyError("evaluate_trend: level должен быть TrendLevel")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise TrendPolicyError("evaluate_trend: entity_id должен быть строкой")
    if not isinstance(as_of, date):
        raise TrendPolicyError("evaluate_trend: as_of должен быть date")

    by_week = _index_by_week(points)

    def verdict(
        status: TrendStatus,
        evidence: EvidenceStatus,
        reason: str,
        **extra: object,
    ) -> TrendVerdict:
        return TrendVerdict(
            level=level,
            entity_id=entity_id.strip(),
            entity_name=entity_name,
            status=status,
            action_grade=_resolve_action_grade(level, status),
            evidence=evidence,
            reason=reason,
            **extra,  # type: ignore[arg-type]
        )

    mature = [
        point
        for week_start, point in sorted(by_week.items())
        if week_age_days(week_start, as_of) >= MIN_WEEK_AGE_DAYS
    ]
    if not mature:
        return verdict(
            TrendStatus.INSUFFICIENT,
            EvidenceStatus.UNKNOWN,
            f"{REASON_NO_MATURE_WEEK}:{MIN_WEEK_AGE_DAYS}d",
        )

    recent_point = mature[-1]
    previous_point = by_week.get(recent_point.week_start - timedelta(days=7))
    earlier_point = by_week.get(recent_point.week_start - timedelta(days=14))
    recent = _to_week(recent_point, as_of)
    if previous_point is None:
        return verdict(
            TrendStatus.INSUFFICIENT,
            EvidenceStatus.UNKNOWN,
            f"{REASON_PREV_WEEK_MISSING}:"
            f"{(recent.week_start - timedelta(days=7)).isoformat()}",
            recent=recent,
        )
    previous = _to_week(previous_point, as_of)
    earlier = (
        _to_week(earlier_point, as_of) if earlier_point is not None else None
    )

    # Шаг 3. Неполная неделя не даёт числа — только причину.
    incomplete = [week for week in (recent, previous) if not week.comparable]
    if incomplete:
        details = ";".join(
            f"{week.week_start.isoformat()}={week.not_comparable_reason}"
            for week in incomplete
        )
        return verdict(
            TrendStatus.INSUFFICIENT,
            EvidenceStatus.INCOMPLETE,
            f"{REASON_WEEK_NOT_COMPARABLE}:{details}",
            recent=recent,
            previous=previous,
            earlier=earlier,
        )

    # Шаг 4. Объём. Данные полные, поэтому evidence остаётся COMPLETE —
    # мало не «дырявых», а именно наблюдений.
    recent_leads = recent.leads or 0
    previous_leads = previous.leads or 0
    min_leads = min(recent_leads, previous_leads)
    if min_leads < MIN_WEEK_LEADS:
        return verdict(
            TrendStatus.INSUFFICIENT,
            EvidenceStatus.COMPLETE,
            f"{REASON_VOLUME_BELOW_MIN}:{min_leads}<{MIN_WEEK_LEADS}",
            recent=recent,
            previous=previous,
            earlier=earlier,
            min_leads=min_leads,
        )

    delta_pp = (recent.qual_pct or 0.0) - (previous.qual_pct or 0.0)
    floor_pp = noise_floor_pp(level, min_leads)
    wilson_recent = wilson_interval(recent.quals or 0, recent_leads)
    wilson_previous = wilson_interval(previous.quals or 0, previous_leads)
    separated = _intervals_separated(wilson_recent, wilson_previous)
    # Суммарная дельта за три недели считается только по полной W-3: число,
    # выведенное из дырявой недели, было бы ровно тем, чего мы избегаем.
    total_delta_pp = (
        (recent.qual_pct or 0.0) - earlier.qual_pct
        if earlier is not None and earlier.comparable and earlier.qual_pct is not None
        else None
    )
    measured: dict[str, object] = {
        "recent": recent,
        "previous": previous,
        "earlier": earlier,
        "delta_pp": delta_pp,
        "total_delta_pp": total_delta_pp,
        "noise_floor_pp": floor_pp,
        "min_leads": min_leads,
        "wilson_recent": wilson_recent,
        "wilson_previous": wilson_previous,
        "wilson_separated": separated,
        "cpl_change_pct": _cpl_change_pct(recent, previous),
        # Справочный ROMI: показывается, но на доказательство ниже не влияет —
        # порога шума для него не замерено (см. докстринг модуля).
        "romi_recent": recent.romi_pct,
        "romi_previous": previous.romi_pct,
        "romi_change_pp": _romi_change_pp(recent, previous),
    }

    # Шаг 5. Доказательство устойчивого движения (_find_proof).
    proof = _find_proof(
        level=level,
        delta_pp=delta_pp,
        floor_pp=floor_pp,
        pair_separated=separated,
        recent=recent,
        previous=previous,
        earlier=earlier,
    )
    if proof is None:
        return verdict(
            TrendStatus.PLATEAU,
            EvidenceStatus.COMPLETE,
            _explain_no_proof(delta_pp, floor_pp, separated),
            **measured,
        )
    sustained = proof

    status = TrendStatus.GROWTH if delta_pp > 0 else TrendStatus.DECLINE
    base_reason = (
        REASON_TREND_CONFIRMED
        if level is TrendLevel.ADSET
        else REASON_AD_LEVEL_DIAGNOSTIC
    )
    return verdict(
        status,
        EvidenceStatus.COMPLETE,
        f"{base_reason}:{sustained.value}",
        sustained_by=sustained,
        **measured,
    )


def evaluate_cohort_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    level: TrendLevel,
    as_of: date,
) -> list[TrendVerdict]:
    """Строки ad_weekly_cohorts → вердикты по адсетам (или по объявлениям).

    Группировка: на уровне адсета строки объявлений одной недели складываются
    (aggregate_week_points), на уровне объявления берутся как есть. Строки без
    ключа группировки (adset_id=NULL — объявление без привязки к адсету в этой
    неделе) пропускаются: приписывать их чужому адсету нельзя.
    """
    if not isinstance(level, TrendLevel):
        raise TrendPolicyError("evaluate_cohort_rows: level должен быть TrendLevel")
    id_key = "adset_id" if level is TrendLevel.ADSET else "ad_id"
    name_key = "adset_name" if level is TrendLevel.ADSET else "ad_name"

    grouped: dict[str, dict[date, list[WeekPoint]]] = {}
    names: dict[str, tuple[date, str]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise TrendPolicyError("evaluate_cohort_rows: строка должна быть Mapping")
        entity_id = raw.get(id_key)
        if not isinstance(entity_id, str) or not entity_id.strip():
            continue
        entity_id = entity_id.strip()
        point = parse_week_point(raw)
        grouped.setdefault(entity_id, {}).setdefault(point.week_start, []).append(
            point
        )
        name = raw.get(name_key)
        if isinstance(name, str) and name.strip():
            known = names.get(entity_id)
            # Имя берём по самой свежей неделе: адсеты переименовывают.
            if known is None or point.week_start >= known[0]:
                names[entity_id] = (point.week_start, name.strip())

    verdicts = [
        evaluate_trend(
            [aggregate_week_points(week_points) for week_points in weeks.values()],
            level=level,
            entity_id=entity_id,
            as_of=as_of,
            entity_name=names.get(entity_id, (None, None))[1],
        )
        for entity_id, weeks in sorted(grouped.items())
    ]
    return verdicts


# ---------------------------------------------------------------------------
# Внутренняя механика
# ---------------------------------------------------------------------------

def _resolve_action_grade(level: TrendLevel, status: TrendStatus) -> bool:
    """Право действовать: только адсет и только при подтверждённом тренде.

    Уровень объявления не даёт action-grade НИКОГДА — при любых цифрах.
    Замер показал: на объявлении недельная дельта квала почти чистый шум до
    ~40 лидов в неделю, а таких объявлений единицы.
    """
    if level is not TrendLevel.ADSET:
        return False
    return status in (TrendStatus.GROWTH, TrendStatus.DECLINE)


def _find_proof(
    *,
    level: TrendLevel,
    delta_pp: float,
    floor_pp: float,
    pair_separated: bool,
    recent: TrendWeek,
    previous: TrendWeek,
    earlier: TrendWeek | None,
) -> SustainProof | None:
    """Доказательство устойчивого движения. None — доказательства нет.

    Доказательств два, оба опираются на одну таблицу шума и один критерий
    Уилсона; достаточно любого. Одиночная пара недель сама по себе права
    действовать не даёт ни в одном из них.

    THREE_WEEKS — W-3 → W-2 → W-1 идут В ОДНУ сторону (оба шага строго
    однонаправлены), W-3 полна и не меньше MIN_WEEK_LEADS, СУММАРНАЯ дельта
    W-3 → W-1 перекрывает порог шума своего объёма, а интервалы Уилсона на
    краях (W-3 против W-1) не пересекаются. Именно это доказательство ловит
    выдыхающуюся звезду: 40% → 28% → 16% — каждый шаг 12 п.п. сам по себе
    тонет в шуме (порог 13,8 п.п. на 80–159 лидах), а падение вдвое за три
    недели реально и обязано быть увиденным. Согласованность направления
    двух подряд шагов и выход суммы за 90-й процентиль волатильность даёт
    редко — по замеру это порядка 5% пар против 10% у одиночной.

    MARGIN — третьей недели нет или она несравнима, но дельта пары
    перекрывает порог в SUSTAIN_MARGIN раз при непересечении интервалов
    Уилсона этой пары. Движение слишком велико, чтобы его игнорировать.
    """
    if earlier is not None and earlier.comparable and earlier.qual_pct is not None:
        earlier_leads = earlier.leads or 0
        step_before = (previous.qual_pct or 0.0) - earlier.qual_pct
        total_delta = (recent.qual_pct or 0.0) - earlier.qual_pct
        same_direction = (
            (delta_pp > 0 and step_before > 0)
            or (delta_pp < 0 and step_before < 0)
        )
        span_leads = min(earlier_leads, previous.leads or 0, recent.leads or 0)
        if (
            same_direction
            and earlier_leads >= MIN_WEEK_LEADS
            and abs(total_delta) >= noise_floor_pp(level, span_leads)
            and _intervals_separated(
                wilson_interval(recent.quals or 0, recent.leads or 0),
                wilson_interval(earlier.quals or 0, earlier_leads),
            )
        ):
            return SustainProof.THREE_WEEKS
    if pair_separated and abs(delta_pp) >= floor_pp * SUSTAIN_MARGIN:
        return SustainProof.MARGIN
    return None


def _explain_no_proof(
    delta_pp: float,
    floor_pp: float,
    pair_separated: bool,
) -> str:
    """Почему подтверждения нет — от самой слабой причины к самой сильной."""
    if abs(delta_pp) < floor_pp:
        return f"{REASON_DELTA_BELOW_NOISE}:{abs(delta_pp):.1f}<{floor_pp:.1f}pp"
    if not pair_separated:
        return REASON_WILSON_OVERLAP
    return (
        f"{REASON_NOT_SUSTAINED}:{abs(delta_pp):.1f}pp<"
        f"{floor_pp * SUSTAIN_MARGIN:.1f}pp"
    )


def _intervals_separated(
    first: tuple[float, float],
    second: tuple[float, float],
) -> bool:
    """Интервалы не пересекаются (касание считается пересечением)."""
    return first[0] > second[1] or second[0] > first[1]


def _cpl_change_pct(recent: TrendWeek, previous: TrendWeek) -> float | None:
    """Справочное изменение CPL, %. Ни на что не влияет — второй сигнал."""
    if recent.cpl_usd is None or not previous.cpl_usd:
        return None
    return (recent.cpl_usd - previous.cpl_usd) / previous.cpl_usd * 100.0


def _romi_change_pp(recent: TrendWeek, previous: TrendWeek) -> float | None:
    """Справочная дельта ROMI в процентных пунктах. На вердикт не влияет.

    В п.п., а не в процентах от процента: ROMI уже отношение, и «упал на 30%»
    от 500% и от 50% — это разные вещи, а «упал на 300 п.п.» читается однозначно.
    Сравнивать можно только недели с ОДИНАКОВЫМ горизонтом выручки: ROMI за 14
    и за 30 дней — разные метрики.
    """
    if recent.romi_pct is None or previous.romi_pct is None:
        return None
    if recent.revenue_horizon_days != previous.revenue_horizon_days:
        return None
    return recent.romi_pct - previous.romi_pct


def _to_week(point: WeekPoint, as_of: date) -> TrendWeek:
    age_days = week_age_days(point.week_start, as_of)
    horizon = point.revenue_horizon_days
    # Сколько дней горизонта прошло с воскресенья недели: горизонт считается от
    # самого позднего лида когорты, поэтому отсчёт от week_end, а не week_start.
    days_elapsed = (
        None if horizon is None else max(0, min(age_days, horizon))
    )
    return TrendWeek(
        week_start=point.week_start,
        week_end=week_end_of(point.week_start),
        age_days=age_days,
        leads=point.amo_leads,
        quals=point.quals,
        qual_pct=point.qual_pct,
        spend_usd=point.spend_usd,
        cpl_usd=point.cpl_usd,
        comparable=point.comparable,
        not_comparable_reason=point.not_comparable_reason,
        revenue_lcy=point.revenue_lcy,
        romi_pct=point.romi_pct,
        revenue_mature=point.revenue_mature,
        revenue_horizon_days=horizon,
        revenue_days_elapsed=days_elapsed,
    )


def _index_by_week(points: Iterable[WeekPoint]) -> dict[date, WeekPoint]:
    """Точки по неделям. Дубль недели — ошибка входа, а не тихое слипание."""
    indexed: dict[date, WeekPoint] = {}
    for point in points:
        if not isinstance(point, WeekPoint):
            raise TrendPolicyError("evaluate_trend: точка должна быть WeekPoint")
        if point.week_start in indexed:
            raise TrendPolicyError(
                f"evaluate_trend: неделя {point.week_start.isoformat()} "
                "передана дважды — сложите её через aggregate_week_points"
            )
        indexed[point.week_start] = point
    return indexed


def _sum_or_none(values: Iterable[int | float | None]):
    """Сумма, в которой один None обнуляет знание обо всей сумме."""
    total: int | float = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _single_value_or_none(values: Iterable[object]):
    """Общее значение, если оно у всех одно и то же. Иначе None.

    Курс недели и горизонт выручки — свойства НЕДЕЛИ, а не объявления: у всех
    строк одной недели они обязаны совпадать. Расхождение (пересчёт со сменой
    горизонта, частично обновлённая неделя) означает, что сумма собрана из
    разных метрик, — тогда честный ответ «неизвестно», а не «возьмём первое».
    """
    seen = None
    for value in values:
        if value is None:
            return None
        if seen is None:
            seen = value
        elif value != seen:
            return None
    return seen


# ---------------------------------------------------------------------------
# Валидация примитивов
# ---------------------------------------------------------------------------

def _require_date(value: object, field: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise TrendPolicyError(f"{field}: невалидная дата {value!r}") from exc
    raise TrendPolicyError(f"{field} должен быть date или 'YYYY-MM-DD'")


def _require_flag(value: object, field: str) -> bool:
    """0/1/bool — как comparable приходит из SQLite. Прочее — ошибка."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise TrendPolicyError(f"{field} должен быть 0, 1 или bool")


def _optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TrendPolicyError(f"{field} должен быть int или None")
    if value < 0:
        raise TrendPolicyError(f"{field} должен быть >= 0")
    return value


def _optional_float(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrendPolicyError(f"{field} должен быть числом или None")
    if not math.isfinite(value) or value < 0:
        raise TrendPolicyError(f"{field} должен быть конечным числом >= 0")
    return float(value)


def _optional_signed_float(value: object, field: str) -> float | None:
    """Число любого знака. Для выручки: возврат может увести её в минус."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrendPolicyError(f"{field} должен быть числом или None")
    if not math.isfinite(value):
        raise TrendPolicyError(f"{field} должен быть конечным числом")
    return float(value)


def _optional_positive_float(value: object, field: str) -> float | None:
    """Строго положительное число. Курс 0 — это не курс, а деление на ноль."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrendPolicyError(f"{field} должен быть числом или None")
    if not math.isfinite(value) or value <= 0:
        raise TrendPolicyError(f"{field} должен быть конечным числом > 0")
    return float(value)


def _optional_flag(value: object, field: str) -> bool:
    """0/1/bool/None → bool. None = «нет колонки» = False (не дозрело)."""
    if value is None:
        return False
    return _require_flag(value, field)


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TrendPolicyError(f"{field} должен быть строкой или None")
    return value.strip() or None


def _validate_optional_nonnegative_int(value: int | None, field: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TrendPolicyError(f"{field} должен быть int >= 0")


def _validate_optional_nonnegative_float(value: float | None, field: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise TrendPolicyError(f"{field} должен быть конечным числом >= 0")


def _validate_optional_signed_float(value: float | None, field: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise TrendPolicyError(f"{field} должен быть конечным числом")


def _validate_optional_positive_float(value: float | None, field: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise TrendPolicyError(f"{field} должен быть конечным числом > 0")
