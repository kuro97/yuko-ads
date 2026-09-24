"""Канонический разбор lead actions из Meta Insights API."""

from dataclasses import dataclass
import math
import re
from typing import Literal


LEAD_ACTION_TYPE = "lead"
INSTANT_FORM_ACTION_TYPE = "onsite_conversion.lead_grouped"
WEBSITE_LEAD_ACTION_TYPES = (
    "onsite_web_lead",
    "offsite_conversion.fb_pixel_lead",
    "offsite_lead_add_20_s_calls",
)
_RELEVANT_ACTION_TYPES = {
    LEAD_ACTION_TYPE,
    INSTANT_FORM_ACTION_TYPE,
    *WEBSITE_LEAD_ACTION_TYPES,
}

ParseStatus = Literal["ok", "component_mismatch", "invalid"]


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Результат разбора Meta lead actions без двойного счёта."""

    canonical_total: int | None
    instant_form: int | None
    website: int | None
    status: ParseStatus
    problems: tuple[str, ...]


def _parse_nonnegative_int(value: object) -> int | None:
    """Читает целое >= 0 без округления и потери дробной части."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0 or not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not re.fullmatch(r"\+?\d+", stripped):
            return None
        try:
            return int(stripped)
        except (ValueError, OverflowError):
            return None
    return None


def parse_meta_lead_actions(actions: object) -> ParseResult:
    """Разбирает actions и возвращает каноническое число Meta-лидов.

    ``lead`` считается авторитетным агрегатом, когда он присутствует. Без него
    итог складывается из instant-form компонента и одного согласованного website
    alias. Пустые/отсутствующие actions означают доказанный ноль, а битая форма,
    дубли релевантных типов и расходящиеся website aliases делают результат
    невалидным.
    """
    if actions is None or actions == []:
        return ParseResult(0, None, None, "ok", ())
    if not isinstance(actions, list):
        return ParseResult(None, None, None, "invalid", ("actions_not_list",))

    values: dict[str, int] = {}
    problems: list[str] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            problems.append(f"action_not_object:{index}")
            continue
        action_type = action.get("action_type")
        if not isinstance(action_type, str):
            problems.append(f"action_type_invalid:{index}")
            continue
        if action_type not in _RELEVANT_ACTION_TYPES:
            continue
        if action_type in values:
            problems.append(f"duplicate_action_type:{action_type}")
            continue
        parsed_value = _parse_nonnegative_int(action.get("value"))
        if parsed_value is None:
            problems.append(f"invalid_action_value:{action_type}")
            continue
        values[action_type] = parsed_value

    instant_form = values.get(INSTANT_FORM_ACTION_TYPE)
    website_values = {
        action_type: values[action_type]
        for action_type in WEBSITE_LEAD_ACTION_TYPES
        if action_type in values
    }
    website: int | None = None
    if website_values:
        distinct_website_values = set(website_values.values())
        if len(distinct_website_values) > 1:
            problems.append("website_alias_mismatch")
        else:
            website = next(iter(distinct_website_values))

    if problems:
        return ParseResult(None, instant_form, website, "invalid", tuple(problems))

    aggregate = values.get(LEAD_ACTION_TYPE)
    component_total = (instant_form or 0) + (website or 0)
    if aggregate is None:
        return ParseResult(component_total, instant_form, website, "ok", ())

    if (instant_form is not None or website is not None) and aggregate != component_total:
        problem = f"component_total_mismatch:lead={aggregate},components={component_total}"
        return ParseResult(aggregate, instant_form, website, "component_mismatch", (problem,))

    return ParseResult(aggregate, instant_form, website, "ok", ())
